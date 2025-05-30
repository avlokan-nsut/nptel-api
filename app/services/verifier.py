from fastapi import HTTPException, status
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from typing import Tuple, Optional, cast

from app.models import Request, RequestStatus, Certificate

from .utils.qr_extraction import extract_link
from .utils.downloader import download_verification_pdf
from .utils.extractor import extract_student_info_from_pdf

import tempfile

class Verifier:
    def __init__(self, uploaded_file_path: str, request_id: str, student_id: str, db: Session):
        self.uploaded_file_path = uploaded_file_path
        self.request_id = request_id
        self.student_id = student_id
        self.db = db
        self.verification_filename = None
    
    async def start_verification(self) -> None:
        # update db request status to processing
        db_request = self.db.query(Request).filter(
            Request.id == self.request_id,
            Request.student_id == self.student_id
        ).first()

        if not db_request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Request not found or does not belong to the current student"
            ) 
        
        offset_aware_due_date = db_request.due_date.replace(tzinfo=timezone.utc) if db_request.due_date else None
        print(f"Offset aware due date: {offset_aware_due_date}")

        if offset_aware_due_date and datetime.now(timezone.utc) > offset_aware_due_date: 
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Request is past the due date"
            )

        db_request.status = RequestStatus.processing

        # add the uploaded certificate details to the db

        db_certificate = self.db.query(Certificate).filter(
            Certificate.request_id == self.request_id
        ).first()

        if not db_certificate:
            db_certificate = Certificate(
                request_id=self.request_id,
                student_id=self.student_id,
                file_url=self.uploaded_file_path,
                verified=False,
            )
            self.db.add(db_certificate)

        try:
            self.db.commit()
            self.db.refresh(db_request)
            self.db.refresh(db_certificate)
        except Exception as e:
            self.db.rollback()
            self.update_status_to_error(db_request, db_certificate, "An internal server error occurred")
            raise e

        verification_link = extract_link(self.uploaded_file_path, 0)
        if not verification_link:
            self.update_status_to_rejected(db_request, db_certificate, "Verification link / QR not found")
            return

        with tempfile.NamedTemporaryFile(mode='w+', delete=True, suffix=".pdf", prefix="certificate_") as temp_f:
            print(f"Temporary file created at: {temp_f.name}")
            success, pdf_url, output = await download_verification_pdf(verification_link, temp_f.name)

            if not success:
                self.update_status_to_error(db_request, db_certificate, "Could not download the verification pdf")
                return 

            db_certificate.verification_file_url = pdf_url

            self.db.commit()
            self.db.refresh(db_request)
            self.db.refresh(db_certificate)
            

            success, output, verified_roll_no, verified_total_marks = self.verify_file(
                temp_f.name,
                cast(str, db_request.subject.name),
                cast(str, db_request.student.name),
            )

            if not success:
                self.update_status_to_rejected(db_request, db_certificate, output)
                return
            
            # Now, since verification has been done, update the final status to all good
            db_request.status = RequestStatus.completed
            db_certificate.verified = True
            db_certificate.remark = "Verification successful"
            self.db.commit()
    
    def verify_file(
        self, 
        verification_file_path: str, 
        subject_name: str, 
        student_name: str
    )-> Tuple[bool, str, Optional[str], Optional[str]]:
        (
            uploaded_course_name,
            uploaded_student_name,
            uploaded_total_marks,
            uploaded_roll_number,
        ) = extract_student_info_from_pdf(self.uploaded_file_path)

        valid_course_name, valid_student_name, valid_total_marks, valid_roll_number = (
            extract_student_info_from_pdf(verification_file_path)
        )

        if (
            uploaded_course_name is None
            or uploaded_student_name is None
            or uploaded_total_marks is None
            or uploaded_roll_number is None
        ):
            return False, "Invalid PDF uploaded. Data missing.", None, None
        
        if (
            valid_course_name is None
            or valid_student_name is None
            or valid_total_marks is None
            or valid_roll_number is None
        ):
            return False, "Invalid details in the verification file", None, None

        print(
            f"Uploaded: {uploaded_course_name}, {uploaded_student_name}, {uploaded_total_marks}, {uploaded_roll_number}"
        )
        print(
            f"Valid: {valid_course_name}, {valid_student_name}, {valid_total_marks}, {valid_roll_number}"
        )

        if (uploaded_course_name.lower().strip() != valid_course_name.lower().strip()) or (
            uploaded_course_name.lower().strip() != subject_name.lower().strip()
        ):
            return False, "Course name mismatch", None, None

        if (uploaded_student_name.lower().strip() != valid_student_name.lower().strip()) or (
            uploaded_student_name.lower().strip() != student_name.lower().strip()
        ):
            return False, "Student name mismatch", None, None

        if uploaded_total_marks != valid_total_marks:
            return False, "Total marks mismatch", None, None

        if uploaded_roll_number != valid_roll_number:
            return False, "Roll number mismatch", None, None


        return (
            True,
            "Verification successful",
            valid_roll_number,
            valid_total_marks,
        )

    
    def update_status_to_rejected(
        self, 
        db_request: Request, 
        db_certificate: Certificate, 
        remark: str
    ) -> None:
        db_request.status = RequestStatus.rejected
        db_certificate.verified = False
        db_certificate.remark = remark

        self.db.commit()
        
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=remark)
    
    def update_status_to_error(
        self, 
        db_request: Request, 
        db_certificate: Certificate, 
        remark: str
    ) -> None:
        db_request.status = RequestStatus.error
        db_certificate.verified = False
        db_certificate.remark = remark

        self.db.commit()
        
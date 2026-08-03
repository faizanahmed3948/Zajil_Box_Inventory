"""
SQLAlchemy models.

Two tables cover the whole app:

- User: a real relational table, since auth needs proper fields
  (hashed password, role, etc).

- Document: a small generic "document store" that mirrors how the app
  already thinks about its data (collections of flexible JSON records,
  the same mental model the previous Firestore version used). Every
  non-auth collection (boxes, orders, auditLog, invoices, customers,
  companies, settings) is stored here as (collection, doc_id, data).
  This keeps the API tiny and means new fields on the frontend never
  require a migration.
"""
import uuid
from sqlalchemy import Column, String, Integer, JSON, UniqueConstraint
from database import Base


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    uid = Column(String, primary_key=True, default=new_id)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, default="")
    role = Column(String, default="employee")  # "employee" | "admin"
    created_at = Column(Integer, nullable=False)

    def as_doc(self) -> dict:
        return {
            "id": self.uid,
            "uid": self.uid,
            "email": self.email,
            "displayName": self.display_name or "",
            "role": self.role,
            "createdAt": self.created_at,
        }


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("collection", "doc_id", name="uq_collection_doc"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    collection = Column(String, nullable=False, index=True)
    doc_id = Column(String, nullable=False)
    data = Column(JSON, nullable=False, default=dict)

    def as_doc(self) -> dict:
        return {"id": self.doc_id, **(self.data or {})}

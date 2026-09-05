"""Signup and login."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..auth import create_access_token, hash_password, verify_password
from ..db import engine
from ..ratelimit import rate_limit_auth

router = APIRouter(tags=["auth"])

MAX_PASSWORD_BYTES = 72
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)

    @field_validator("password")
    @classmethod
    def fits_bcrypt(cls, value: str) -> str:
        # Characters are not bytes once non-ASCII is involved; bcrypt counts bytes.
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
        return value


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/auth/signup", dependencies=[Depends(rate_limit_auth)])
def signup(body: SignupRequest):
    with engine.begin() as conn:
        try:
            row = conn.execute(
                text(
                    "INSERT INTO users (email, password_hash) VALUES (:email, :hash) RETURNING id"
                ),
                {"email": body.email, "hash": hash_password(body.password)},
            ).mappings().first()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="Email already registered")
    return {"access_token": create_access_token(row["id"]), "token_type": "bearer"}


@router.post("/auth/login", dependencies=[Depends(rate_limit_auth)])
def login(body: LoginRequest):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, password_hash FROM users WHERE email = :email"),
            {"email": body.email},
        ).mappings().first()

    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"access_token": create_access_token(row["id"]), "token_type": "bearer"}

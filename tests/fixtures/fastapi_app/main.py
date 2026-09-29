"""FastAPI fixture used by the scanner tests."""

from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel


class BrandResponse(BaseModel):
    id: int
    name: str
    active: bool = True

app = FastAPI()
router = APIRouter(prefix="/brands")


@app.get("/health")
def health():
    return {"status": "ok"}


@router.get("/{brand_id}", response_model=BrandResponse)
def get_brand(brand_id: int, expand: str = Query(None)) -> BrandResponse:
    return {"id": brand_id, "expand": expand}


@router.post("/")
def create_brand(payload: dict):
    return payload


app.include_router(router)

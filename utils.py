import hashlib
import re
import os
from datetime import datetime


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(url: str) -> str:
    name = re.sub(r"[^\w\-_]", "_", url)
    name = re.sub(r"_+", "_", name)
    if not name.endswith(".html"):
        name += ".html"
    return name


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\- ]", "", text)
    text = re.sub(r"[\-_]+", "-", text)
    text = text.replace(" ", "-")
    return text


def now_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def build_product_url(manufacturer: str, part_number: str) -> str:
    slug_mfr = slugify(manufacturer)
    slug_pn = slugify(part_number)
    return f"https://www.datasheets.com/{slug_mfr}/{slug_pn}"


def build_pdf_url(manufacturer: str, part_number: str) -> str:
    slug_mfr = slugify(manufacturer)
    slug_pn = slugify(part_number)
    return f"https://www.datasheets.com/{slug_mfr}/{slug_pn}/datasheet.pdf"


def ensure_dirs(*dirs):
    for d in dirs:
        os.makedirs(d, exist_ok=True)

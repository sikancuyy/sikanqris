"""
QRIS Service Module
Menangani logika pembuatan QRIS untuk GoPay Merchant:
1. Mode GOPAY_STATIC: Mengonversi QRIS Statis GoPay Merchant (GoBiz) menjadi QRIS Dinamis resmi ber-nominal sesuai standar EMVCo / Bank Indonesia & ASPI.
2. Mode MIDTRANS_GOPAY: Menggunakan Midtrans Core API (GoPay Official Partner Gateway).
3. Mode GENERIC_API: Menggunakan endpoint REST API pihak ketiga jika disediakan.
"""

import io
import os
import re
import logging
from dataclasses import dataclass
from typing import Optional
import httpx
import qrcode
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

# Pastikan environment variables selalu dimuat
load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class QRISResult:
    """Hasil generate QRIS yang valid"""
    nominal: int
    qr_string: str
    image_bytes: bytes


class QRISServiceError(Exception):
    """Exception khusus untuk error pembuatan QRIS"""
    pass


class QRISConfigurationError(QRISServiceError):
    """Exception jika konfigurasi / kredensial belum diisi"""
    pass


def calculate_crc16(data: str) -> str:
    """
    Menghitung checksum CRC-16 (CCITT-FALSE) standar EMVCo Bank Indonesia.
    Polynomial: 0x1021, Initial: 0xFFFF
    """
    crc = 0xFFFF
    for b in data.encode("utf-8"):
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def convert_static_to_dynamic(static_qris: str, nominal: int) -> str:
    """
    Mengonversi string QRIS Statis (misalnya dari GoPay Merchant / GoBiz)
    menjadi QRIS Dinamis ber-nominal dengan menyuntikkan Tag 54 (Nominal)
    dan menghitung ulang Tag 63 (CRC-16) standar EMVCo Bank Indonesia & ASPI.
    
    Menggunakan parser TLV sekuensial agar ID Merchant (seperti Tag 51)
    tidak terpotong atau rusak.
    """
    clean = static_qris.strip()
    if not clean:
        raise QRISConfigurationError("String QRIS Statis GoPay belum diisi di file .env")

    if not clean.startswith("000201"):
        raise QRISServiceError("Format string QRIS tidak valid (harus diawali '000201')")

    # Hapus checksum lama jika ada di akhir (Tag 63)
    idx_63 = clean.rfind("6304")
    if idx_63 != -1 and len(clean) - idx_63 <= 8:
        clean = clean[:idx_63]

    # Parsing TLV sekuensial tingkat root
    idx = 0
    tags = []
    while idx < len(clean):
        if idx + 4 > len(clean):
            break
        tag = clean[idx:idx+2]
        length_str = clean[idx+2:idx+4]
        if not length_str.isdigit():
            break
        length = int(length_str)
        val = clean[idx+4:idx+4+length]
        tags.append((tag, val))
        idx += 4 + length

    # Bangun kembali tag dengan Tag 01 (Dynamic) dan Tag 54 (Transaction Amount)
    new_tags = []
    nominal_str = str(nominal)
    tag_54_inserted = False

    for tag, val in tags:
        if tag == "01":
            new_tags.append(("01", "12"))  # Ubah dari 11 (Statis) ke 12 (Dinamis)
        elif tag == "54":
            continue  # Hapus tag nominal lama jika ada di level root
        elif tag in ("58", "59", "60", "61", "62"):
            # Sisipkan Tag 54 sebelum Tag 58 (Country Code) sesuai standar EMVCo
            if not tag_54_inserted:
                new_tags.append(("54", nominal_str))
                tag_54_inserted = True
            new_tags.append((tag, val))
        else:
            new_tags.append((tag, val))

    if not tag_54_inserted:
        new_tags.append(("54", nominal_str))

    # Gabungkan semua tag
    payload = ""
    for tag, val in new_tags:
        payload += f"{tag}{len(val):02d}{val}"

    # Tambahkan Tag 63 identifier (6304) lalu hitung CRC-16
    payload_for_crc = payload + "6304"
    crc = calculate_crc16(payload_for_crc)

    return payload_for_crc + crc


def get_bold_font(font_size: int):
    """Mendapatkan font bold dari sistem Windows dengan fallback aman"""
    candidate_fonts = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for font_path in candidate_fonts:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, font_size)
            except Exception:
                pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def render_qr_image(
    payload: str,
    nominal: Optional[int] = None,
    frame_path: Optional[str] = None
) -> bytes:
    """
    Membuat gambar QR Code beresolusi tinggi dalam format PNG (in-memory BytesIO)
    dari string payload QRIS.
    
    Mendukung template bingkai resmi:
    1. bqr.png (1792 x 2400):
       - QR Code dipaskan di kotak putih tengah (x=895, y=988, ukuran 920x920)
       - Nominal rupiah ditulis di shape warna dongker (navy blue) di bawah QRIS
         dengan teks putih tebal (bold) terpusat sempurna.
    2. bingkai-qris.png (1000 x 1000):
       - Bingkai persegi dengan QR code terpusat.
    3. Standar tanpa bingkai jika file bingkai tidak tersedia.
    """
    from PIL import ImageDraw

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Tentukan path bingkai (prioritas: parameter -> bqr.png -> QRIS_FRAME_PATH -> bingkai-qris.png)
    if frame_path is None:
        env_frame = os.getenv("QRIS_FRAME_PATH", "").strip()
        if env_frame:
            frame_path = env_frame
        elif os.path.exists(os.path.join(base_dir, "bqr.png")):
            frame_path = "bqr.png"
        else:
            frame_path = "bingkai-qris.png"

    if not os.path.isabs(frame_path):
        resolved_frame = os.path.join(base_dir, frame_path)
    else:
        resolved_frame = frame_path

    has_frame = os.path.exists(resolved_frame)

    # Buat QR code
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1 if has_frame else 2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    if has_frame:
        try:
            frame_img = Image.open(resolved_frame).convert("RGBA")
            fw, fh = frame_img.size

            # Mode 1: Template bqr.png (Resolusi ~1792x2400 atau rasio tinggi)
            if fh > fw * 1.2:
                # Kotak putih QR pada bqr.png berada di x=355..1435, y=490..1486 (center: 895, 988)
                qr_size = 920
                qr_resized = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
                qr_x = 895 - qr_size // 2
                qr_y = 988 - qr_size // 2

                composite = frame_img.copy()
                composite.paste(qr_resized, (qr_x, qr_y))

                # Tambahkan nominal di shape warna dongker (navy blue) di bawah QRIS
                if nominal is not None:
                    draw = ImageDraw.Draw(composite)
                    font_size = 72
                    font = get_bold_font(font_size)
                    nominal_text = f"Rp {nominal:,}".replace(",", ".")

                    if font:
                        # Auto-scale font jika teks sangat panjang agar tidak meluber
                        bbox = draw.textbbox((0, 0), nominal_text, font=font)
                        text_w = bbox[2] - bbox[0]
                        if text_w > 850:
                            font_size = int(font_size * 850 / text_w)
                            font = get_bold_font(font_size)

                        # Shape warna dongker berpusat di x=896, y=1550
                        draw.text(
                            (896, 1550),
                            nominal_text,
                            fill=(255, 255, 255, 255),
                            font=font,
                            anchor="mm",
                        )

                buffer = io.BytesIO()
                composite.convert("RGB").save(buffer, format="PNG")
                buffer.seek(0)
                return buffer.getvalue()

            # Mode 2: Template bingkai persegi (1000x1000)
            else:
                target_size = (1000, 1000)
                frame_resized = frame_img.resize(target_size, Image.Resampling.LANCZOS)
                qr_size = 760
                qr_img_resized = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

                pos_x = (target_size[0] - qr_size) // 2
                pos_y = (target_size[1] - qr_size) // 2

                composite = frame_resized.copy()
                composite.paste(qr_img_resized, (pos_x, pos_y))

                buffer = io.BytesIO()
                composite.save(buffer, format="PNG")
                buffer.seek(0)
                return buffer.getvalue()

        except Exception as exc:
            logger.warning(f"Gagal memuat bingkai QRIS ({exc}), beralih ke QR standar.")

    # Fallback ke QR standar tanpa bingkai
    buffer = io.BytesIO()
    qr_img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.getvalue()


async def generate_qris_midtrans(nominal: int, server_key: str, is_production: bool = False) -> QRISResult:
    """
    Membuat QRIS resmi melalui Midtrans Core API (GoPay Partner Payment Gateway).
    Endpoint: https://api.midtrans.com/v2/charge (atau sandbox)
    """
    base_url = "https://api.midtrans.com" if is_production else "https://api.sandbox.midtrans.com"
    endpoint = f"{base_url}/v2/charge"

    import time
    import uuid
    order_id = f"QRIS-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    payload = {
        "payment_type": "qris",
        "transaction_details": {
            "order_id": order_id,
            "gross_amount": nominal,
        },
        "qris": {
            "acquirer": "gopay",
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                endpoint,
                json=payload,
                auth=(server_key, ""),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        except Exception as exc:
            logger.error(f"Gagal menghubungi Midtrans API: {exc}")
            raise QRISServiceError(f"Gagal menghubungi server pembayaran: {exc}")

    if response.status_code not in (200, 201):
        logger.error(f"Midtrans API error [{response.status_code}]: {response.text}")
        raise QRISServiceError(f"Respon penyedia QRIS tidak valid: HTTP {response.status_code}")

    data = response.json()
    status_code = data.get("status_code")
    if status_code not in ("200", "201"):
        err_msg = data.get("status_message", "Gagal membuat transaksi QRIS")
        logger.error(f"Midtrans error: {err_msg}")
        raise QRISServiceError(f"Midtrans error: {err_msg}")

    # Midtrans menyediakan qr_string atau action link image
    qr_string = data.get("qr_string")
    if qr_string:
        image_bytes = render_qr_image(qr_string, nominal=nominal)
        return QRISResult(nominal=nominal, qr_string=qr_string, image_bytes=image_bytes)

    # Cek jika ada link gambar langsung di actions
    actions = data.get("actions", [])
    for action in actions:
        if action.get("name") == "generate-qr-code":
            qr_url = action.get("url")
            async with httpx.AsyncClient(timeout=30.0) as client:
                img_res = await client.get(qr_url)
                if img_res.status_code == 200:
                    return QRISResult(nominal=nominal, qr_string=qr_url, image_bytes=img_res.content)

    raise QRISServiceError("API Midtrans tidak mengembalikan data QRIS yang valid")


async def generate_qris_generic_api(nominal: int, api_url: str, api_key: str) -> QRISResult:
    """
    Membuat QRIS melalui generic REST API.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"nominal": nominal}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(api_url, json=payload, headers=headers)
        except Exception as exc:
            logger.error(f"Gagal menghubungi QRIS Generic API: {exc}")
            raise QRISServiceError(f"Koneksi ke API QRIS gagal: {exc}")

    if response.status_code != 200:
        logger.error(f"QRIS API HTTP {response.status_code}: {response.text}")
        raise QRISServiceError(f"API QRIS mengembalikan error HTTP {response.status_code}")

    try:
        data = response.json()
    except Exception:
        raise QRISServiceError("Respon API bukan format JSON yang valid")

    # Cek format respon payload teks atau image URL
    qr_string = data.get("qr_string") or data.get("payload") or data.get("data", {}).get("qr_string")
    if qr_string:
        image_bytes = render_qr_image(qr_string, nominal=nominal)
        return QRISResult(nominal=nominal, qr_string=qr_string, image_bytes=image_bytes)

    qr_image_url = data.get("qr_image") or data.get("data", {}).get("qr_image")
    if qr_image_url:
        async with httpx.AsyncClient(timeout=30.0) as client:
            img_res = await client.get(qr_image_url)
            if img_res.status_code == 200:
                return QRISResult(nominal=nominal, qr_string=qr_image_url, image_bytes=img_res.content)

    raise QRISServiceError("API tidak mengembalikan field qr_string atau qr_image yang valid")


async def generate_qris(nominal: int) -> QRISResult:
    """
    Fungsi utama untuk membuat QRIS berdasarkan nominal.
    Menyesuaikan dengan konfigurasi di .env:
    1. QRIS_MODE = GOPAY_STATIC (Menggunakan string QRIS statis GoPay Merchant)
    2. QRIS_MODE = MIDTRANS_GOPAY (Menggunakan Midtrans Core API)
    3. QRIS_MODE = GENERIC_API (Menggunakan REST API pihak ketiga)
    """
    if nominal <= 0:
        raise ValueError("Nominal QRIS harus lebih besar dari 0")

    mode = os.getenv("QRIS_MODE", "GOPAY_STATIC").strip().upper()

    if mode == "GOPAY_STATIC":
        static_qris = os.getenv("GOPAY_QRIS_STATIC_STRING", "").strip()
        if not static_qris:
            logger.warning("GOPAY_QRIS_STATIC_STRING belum diset di .env")
            raise QRISConfigurationError(
                "Kredensial QRIS GoPay belum disetel. "
                "Harap masukkan string QRIS GoPay Merchant Anda pada file .env (GOPAY_QRIS_STATIC_STRING)."
            )
        # Generate dynamic QRIS string
        dynamic_qris = convert_static_to_dynamic(static_qris, nominal)
        # Render into image bytes dengan bingkai dan nominal
        image_bytes = render_qr_image(dynamic_qris, nominal=nominal)
        logger.info(f"Berhasil membuat QRIS Dinamis GoPay untuk nominal Rp{nominal:,}")
        return QRISResult(nominal=nominal, qr_string=dynamic_qris, image_bytes=image_bytes)

    elif mode == "MIDTRANS_GOPAY":
        server_key = os.getenv("MIDTRANS_SERVER_KEY", "").strip()
        if not server_key:
            raise QRISConfigurationError("MIDTRANS_SERVER_KEY belum diisi pada file .env")
        is_production = os.getenv("MIDTRANS_IS_PRODUCTION", "false").strip().lower() == "true"
        return await generate_qris_midtrans(nominal, server_key, is_production)

    elif mode == "GENERIC_API":
        api_url = os.getenv("QRIS_API_URL", "").strip()
        api_key = os.getenv("QRIS_API_KEY", "").strip()
        if not api_url:
            raise QRISConfigurationError("QRIS_API_URL belum diisi pada file .env")
        return await generate_qris_generic_api(nominal, api_url, api_key)

    else:
        raise QRISConfigurationError(f"Mode QRIS '{mode}' tidak dikenali. Pilih GOPAY_STATIC atau MIDTRANS_GOPAY.")

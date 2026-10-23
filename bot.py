"""
QRIS Generator Bot
Telegram Bot khusus untuk generate QRIS berdasarkan nominal rupiah menggunakan python-telegram-bot v21+.
"""

import asyncio
import logging
import os
import re
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from qris_service import QRISConfigurationError, QRISServiceError, generate_qris

# Load environment variables dari file .env
load_dotenv()

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# State percakapan
STATE_WAITING_NOMINAL = 1


def format_rupiah(nominal: int) -> str:
    """Format angka nominal ke format Rupiah standar Indonesia (misal: Rp10.000)"""
    formatted = f"{nominal:,}".replace(",", ".")
    return f"Rp{formatted}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    1. MENU AWAL
    Menampilkan pesan sambutan dan tombol inline [🔳 GENERATE QRIS]
    """
    text = (
        "Selamat datang di QRIS Generator Bot.\n\n"
        "Tekan tombol di bawah untuk membuat QRIS."
    )
    keyboard = [
        [InlineKeyboardButton("🔳 GENERATE QRIS", callback_data="btn_generate")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)

    # Akhiri conversation jika sebelumnya ada state aktif
    return ConversationHandler.END


async def ask_nominal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    2. KLIK GENERATE QRIS / GENERATE LAGI
    Menampilkan pertanyaan untuk memasukkan nominal rupiah
    """
    query = update.callback_query
    await query.answer()

    text = (
        "💰 Masukkan nominal QRIS yang ingin dibuat.\n\n"
        "Contoh: 10000\n\n"
        "Masukkan nominal dalam rupiah tanpa titik atau koma."
    )
    await query.message.reply_text(text)

    # Masuk ke state menunggu input nominal
    return STATE_WAITING_NOMINAL


async def handle_nominal_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    3. INPUT NOMINAL & 4. PROSES GENERATE QRIS
    - Memvalidasi input hanya berupa angka dan > 0
    - Mengirim ke QRIS service
    - Menampilkan hasil QRIS jika berhasil atau pesan error jika gagal
    """
    user_text = update.message.text.strip()
    user_id = update.effective_user.id
    logger.info(f"User {user_id} memasukkan input nominal: {user_text}")

    # Validasi hanya angka (digit) tanpa huruf, titik, koma, minus, atau simbol
    if not re.fullmatch(r"^[0-9]+$", user_text):
        error_msg = (
            "⚠️ Input tidak valid.\n\n"
            "Harap masukkan angka saja tanpa titik atau koma (minimal 1).\n"
            "Contoh: 10000"
        )
        await update.message.reply_text(error_msg)
        # Tetap di state menunggu nominal yang benar
        return STATE_WAITING_NOMINAL

    nominal = int(user_text)

    # Validasi nominal harus lebih besar dari 0
    if nominal <= 0:
        error_msg = (
            "⚠️ Nominal harus lebih besar dari 0.\n\n"
            "Contoh: 10000"
        )
        await update.message.reply_text(error_msg)
        return STATE_WAITING_NOMINAL

    # Keyboard tombol [🔄 GENERATE LAGI]
    regenerate_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 GENERATE LAGI", callback_data="btn_regenerate")]
    ])

    # Kirim indikasi sedang memproses
    processing_msg = await update.message.reply_text("⏳ Sedang memproses QRIS...")

    try:
        # Panggil service generate QRIS
        qris_result = await generate_qris(nominal)

        # Hapus pesan processing
        try:
            await processing_msg.delete()
        except Exception:
            pass

        # 5. JIKA GENERATE QRIS BERHASIL
        caption_text = (
            "✅ QRIS BERHASIL\n\n"
            f"💰 Nominal: {format_rupiah(nominal)}\n\n"
            "Silakan pindai QRIS di atas menggunakan aplikasi pembayaran yang mendukung QRIS."
        )

        await update.message.reply_photo(
            photo=qris_result.image_bytes,
            caption=caption_text,
            reply_markup=regenerate_keyboard,
        )
        logger.info(f"QRIS berhasil dikirim ke user {user_id} dengan nominal {nominal}")

    except (QRISServiceError, QRISConfigurationError, Exception) as exc:
        logger.error(f"Gagal generate QRIS untuk user {user_id}: {exc}")

        # Hapus pesan processing jika masih ada
        try:
            await processing_msg.delete()
        except Exception:
            pass

        # 6. JIKA GENERATE QRIS GAGAL
        fail_text = (
            "❌ QRIS GAGAL DIBUAT\n\n"
            "Terjadi kesalahan saat membuat QRIS.\n"
            "Silakan coba kembali."
        )
        await update.message.reply_text(
            fail_text,
            reply_markup=regenerate_keyboard,
        )

    # Setelah proses selesai (berhasil / gagal), selesaikan percakapan
    # Pengguna dapat menekan tombol [🔄 GENERATE LAGI] untuk memulai kembali
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membatalkan proses jika pengguna mengetik /cancel"""
    await update.message.reply_text(
        "Proses dibatalkan. Ketik /start untuk kembali ke menu awal."
    )
    return ConversationHandler.END


def main():
    """Inisialisasi dan jalankan Telegram Bot"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    if not bot_token:
        logger.critical("TELEGRAM_BOT_TOKEN tidak ditemukan di file .env!")
        print("ERROR: Harap isi TELEGRAM_BOT_TOKEN di file .env terlebih dahulu.")
        return

    # Kompatibilitas Python 3.14: Inisialisasi event loop pada MainThread sebelum ApplicationBuilder
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    logger.info("Memulai QRIS Generator Bot...")

    application = ApplicationBuilder().token(bot_token).build()

    # Conversation handler untuk alur generate QRIS
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_nominal_callback, pattern=r"^(btn_generate|btn_regenerate)$"),
            CommandHandler("start", start_command),
        ],
        states={
            STATE_WAITING_NOMINAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nominal_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
            CallbackQueryHandler(ask_nominal_callback, pattern=r"^(btn_generate|btn_regenerate)$"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start_command))

    logger.info("Bot sedang berjalan (polling mode)...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

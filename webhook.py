import os
import re
import logging
from flask import Flask, request, jsonify
import config
import database as db

logger = logging.getLogger(__name__)
app = Flask(__name__)

# Regex patterns for SMS parsing
AMOUNT_RE = re.compile(r"(?:Tk|BDT|Rs)\s*([\d,]+\.?\d*)", re.I)
TRX_RE = re.compile(r"TrxID\s*[:\-\s]*([A-Za-z0-9]{6,})", re.I)
TRX_ALT_RE = re.compile(r"TxnID\s*[:\-\s]*([A-Za-z0-9]{6,})", re.I)
TRXID_GENERIC = re.compile(r"(?:Trx|Txn|Transaction)\s*ID\s*[:\-\s]*([A-Za-z0-9]{6,})", re.I)

GATEWAY_MAP = {
    "bKash": ["bkash", "16247"],
    "Rocket": ["rocket", "16216"],
}

def detect_gateway(sender: str, body: str) -> str:
    text = f"{sender} {body}".lower()
    for gw, keys in GATEWAY_MAP.items():
        for k in keys:
            if k.lower() in text:
                return gw
    if "bkash" in text:
        return "bKash"
    if "rocket" in text:
        return "Rocket"
    return "bKash"

def parse_sms(sender: str, body: str):
    gateway = detect_gateway(sender, body)
    # Amount: first match like Tk 500.00 or BDT 500
    m_amt = AMOUNT_RE.search(body)
    amount = None
    if m_amt:
        try:
            amount = float(m_amt.group(1).replace(",", ""))
        except:
            amount = None
    # TrxID
    trx = None
    for pat in (TRX_RE, TRX_ALT_RE, TRXID_GENERIC):
        m = pat.search(body)
        if m:
            trx = m.group(1).strip().upper()
            break
    return gateway, amount, trx

@app.route("/api/sms-webhook", methods=["POST"])
def sms_webhook():
    data = request.get_json(silent=True) or {}
    # Require a valid secret (shared with the SMS forwarder).
    provided = data.get("secret") or request.headers.get("X-Webhook-Secret") or ""
    expected = config.WEBHOOK_SECRET
    if not expected:
        logger.error("WEBHOOK_SECRET is not configured - rejecting webhook.")
        return jsonify({"error": "Webhook not configured"}), 500
    if provided != expected:
        return jsonify({"error": "Unauthorized"}), 401
    # Support multiple payload shapes from SMS forwarder apps
    sender = data.get("sender") or data.get("from") or data.get("address") or ""
    body = data.get("message") or data.get("body") or data.get("text") or ""
    if not body:
        # also try raw text
        body = request.data.decode("utf-8", errors="ignore") if request.data else ""
    if not sender and not body:
        return jsonify({"error": "Missing sender/body"}), 400
    gateway, amount, trx_id = parse_sms(sender, body)
    if not trx_id:
        return jsonify({"error": "Could not parse TrxID", "gateway": gateway}), 400
    if amount is None:
        return jsonify({"error": "Could not parse Amount", "trx_id": trx_id}), 400
    # Save as UNCLAIMED
    row_id = db.create_pending_deposit(gateway, amount, trx_id, sender, body)
    if row_id is None:
        # duplicate
        return jsonify({"status": "duplicate", "trx_id": trx_id}), 200
    logger.info("SMS webhook: saved %s %s %s", gateway, amount, trx_id)
    return jsonify({"status": "UNCLAIMED", "id": row_id, "gateway": gateway, "amount": amount, "trx_id": trx_id}), 201

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

def run_webhook():
    port = int(os.getenv("PORT", "8080"))
    # init db to ensure tables exist
    db.init_db()
    app.run(host="0.0.0.0", port=port)

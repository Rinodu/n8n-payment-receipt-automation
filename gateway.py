"""Midtrans sandbox verification. Provider-specific signature + authoritative status."""
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def get_status(order_id, server_key):
    authorization = base64.b64encode((server_key + ':').encode()).decode()
    request = Request('https://api.sandbox.midtrans.com/v2/' + order_id + '/status',
                      headers={'Authorization': 'Basic ' + authorization, 'Accept': 'application/json'})
    with build_opener(NoRedirect).open(request, timeout=15) as response:
        return json.loads(response.read(65537))


def verify_midtrans(body, server_key, expected_order, fetch=get_status):
    if not isinstance(body, dict):
        raise ValueError('invalid_notification')
    order_id = body.get('order_id')
    if not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', order_id):
        raise ValueError('invalid_order_id')
    parts = [body.get(key) for key in ('order_id', 'status_code', 'gross_amount')]
    signature = body.get('signature_key', '')
    if not all(isinstance(part, str) for part in parts) or not isinstance(signature, str):
        raise ValueError('invalid_signature')
    expected = hashlib.sha512((''.join(parts) + server_key).encode()).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('invalid_signature')
    # Signature doesn't bind every field; never trust email/status from the notification.
    status = fetch(order_id, server_key)
    if status.get('order_id') != order_id or status.get('currency') != 'IDR':
        raise ValueError('gateway_identity_mismatch')
    if status.get('transaction_status') not in ('settlement', 'capture'):
        raise ValueError('gateway_not_paid')
    if status.get('transaction_status') == 'capture' and status.get('fraud_status') != 'accept':
        raise ValueError('gateway_fraud_not_accepted')
    if status.get('fraud_status', 'accept') != 'accept':
        raise ValueError('gateway_fraud_not_accepted')
    try:
        amount = Decimal(status['gross_amount'])
        notified = Decimal(body['gross_amount'])
        if not amount.is_finite() or amount != amount.to_integral_value() or notified != amount or amount != expected_order['amount']:
            raise ValueError('gateway_amount_mismatch')
    except (InvalidOperation, KeyError, TypeError):
        raise ValueError('gateway_amount_mismatch')
    if expected_order['currency'] != 'IDR':
        raise ValueError('order_currency_mismatch')
    # Stable across capture -> settlement notifications for the same transaction.
    paid_at = status.get('transaction_time')
    if not isinstance(paid_at, str):
        raise ValueError('gateway_missing_timestamp')
    paid_at = datetime.strptime(paid_at, '%Y-%m-%d %H:%M:%S').isoformat() + '+07:00'
    return {'payment_id': status.get('transaction_id'), 'order_id': order_id, 'email': expected_order['email'],
            'amount': int(amount), 'currency': 'IDR', 'status': 'paid', 'paid_at': paid_at}

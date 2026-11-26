"""Server-side email OTP. Never log codes or pass them to the browser."""
import hashlib
import hmac
import os
import json
import secrets
import smtplib
import ssl
import threading
import time
import urllib.request
from email.message import EmailMessage

TTL = 300
RESEND_WAIT = 60
MAX_ATTEMPTS = 5
_KEY = secrets.token_bytes(32)
_LOCK = threading.Lock()
_PENDING = {}
_SENDS = []
_FAILURES = []


class OTPError(Exception):
    pass


def enabled():
    return os.environ.get('LOGIN_OTP_ENABLED', '').lower() == 'true'


def recipient():
    return os.environ['LOGIN_OTP_EMAIL']


def masked_recipient():
    name, _, domain = recipient().partition('@')
    return name[:2] + '••••@' + domain


def configured():
    if os.environ.get('RESEND_API_KEY'):
        return bool(os.environ.get('LOGIN_OTP_FROM'))
    return all(os.environ.get(k) for k in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD'))


def _digest(token, code):
    return hmac.new(_KEY, (token + ':' + code).encode(), hashlib.sha256).digest()


def _email(code, description='Sign in to Signal desk'):
    if not configured():
        raise OTPError('Email verification is not configured. Contact the dashboard owner.')
    message = EmailMessage()
    message['Subject'] = 'Your Signal desk verification code'
    message['From'] = os.environ.get('LOGIN_OTP_FROM') or os.environ['SMTP_USER']
    message['To'] = recipient()
    message.set_content(f'Your Signal desk verification code is: {code}\n\n'
                        f'Request: {description}\n\n'
                        'This code expires in 5 minutes and can be used once.\n'
                        'Do not share this code. If you did not request it, ignore this email.')
    try:
        if os.environ.get('RESEND_API_KEY'):
            payload = json.dumps({'from': os.environ['LOGIN_OTP_FROM'], 'to': [recipient()],
                                  'subject': message['Subject'], 'text': message.get_content()}).encode()
            request = urllib.request.Request('https://api.resend.com/emails', data=payload,
                        headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
                                 'Content-Type': 'application/json', 'User-Agent': 'SignalDesk/1.0'}, method='POST')
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status not in (200, 201, 202):
                    raise OTPError('Email provider did not accept the message.')
            return
        port = int(os.environ.get('SMTP_PORT', '587'))
        context = ssl.create_default_context()
        if port == 465:
            connection = smtplib.SMTP_SSL(os.environ['SMTP_HOST'], port, timeout=15, context=context)
        else:
            connection = smtplib.SMTP(os.environ['SMTP_HOST'], port, timeout=15)
        with connection as client:
            if port != 465:
                client.starttls(context=context)
            client.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
            client.send_message(message)
    except Exception:
        # SMTP exceptions can contain credentials or message contents. Keep them out of responses/logs.
        raise OTPError('Could not send the email. Please try again later.') from None


def _cleanup(now):
    for token in list(_PENDING):
        if _PENDING[token]['expires'] <= now:
            del _PENDING[token]
    _SENDS[:] = [t for t in _SENDS if t > now - 3600]
    _FAILURES[:] = [t for t in _FAILURES if t > now - 3600]


def start(previous='', purpose='login', description='Sign in to Signal desk'):
    """The caller must authenticate the login or session before requesting a code."""
    if not configured():
        raise OTPError('Email verification is not configured. Contact the dashboard owner.')
    now = time.time()
    token = secrets.token_urlsafe(32)
    code = f'{secrets.randbelow(1000000):06d}'
    with _LOCK:
        _cleanup(now)
        if len(_FAILURES) >= 10:
            raise OTPError('Too many incorrect codes. Try signing in again in an hour.')
        if len(_SENDS) >= 5:
            raise OTPError('Email limit reached. Try signing in again in an hour.')
        if _SENDS and now - _SENDS[-1] < RESEND_WAIT:
            raise OTPError('Please wait 60 seconds before requesting another code.')
        _SENDS.append(now)
    _email(code, description)
    with _LOCK:
        _PENDING.pop(previous, None)
        _PENDING[token] = {'digest': _digest(token, code), 'expires': time.time() + TTL,
                           'attempts': 0, 'purpose': purpose}
    return token


def pending(token, purpose='login'):
    with _LOCK:
        _cleanup(time.time())
        return token in _PENDING and _PENDING[token]['purpose'] == purpose


def verify(token, code, purpose='login'):
    with _LOCK:
        now = time.time()
        _cleanup(now)
        challenge = _PENDING.get(token)
        restart = 'Sign in again.' if purpose == 'login' else 'Start the change again.'
        if not challenge or challenge['purpose'] != purpose:
            raise OTPError('This code has expired or was already used. ' + restart)
        if len(_FAILURES) >= 10:
            _PENDING.pop(token, None)
            raise OTPError('Too many incorrect codes. Try signing in again in an hour.')
        # Count every submission, including invalid formats, to prevent bypassing the attempt limit.
        challenge['attempts'] += 1
        valid = len(code) == 6 and code.isascii() and code.isdigit()
        if valid and hmac.compare_digest(challenge['digest'], _digest(token, code)):
            del _PENDING[token]
            return True
        _FAILURES.append(now)
        if challenge['attempts'] >= MAX_ATTEMPTS:
            del _PENDING[token]
            raise OTPError('Too many incorrect codes. ' + restart)
        raise OTPError('Incorrect code. Please check your email and try again.')


def cancel(token):
    with _LOCK:
        _PENDING.pop(token, None)

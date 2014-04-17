#!/usr/bin/env python
"""
FlaskCA
"""

import M2Crypto
import ldap
import ldap.filter
import os
import time
import uuid
from collections import namedtuple

from flask import Flask, request

from . import validators

app = Flask(__name__)
app.config.from_pyfile(os.environ.get('EPHEMERAL_CA_SETTINGS', 'config.cfg'))

AUTH_FAILED = object()


AuthDetails = namedtuple('AuthDetails', ['username', 'groups'])


def ldap_user_get_groups(attributes):
    groups = attributes.get('memberOf', [])
    group_dns = [ldap.dn.explode_dn(g, notypes=True) for g in groups]
    return set(x[0] for x in group_dns if x[1] == 'Groups')


def ldap_login(user, secret):
    ldo = ldap.initialize("ldap://%s" % (app.config['LDAP_HOST'],))
    ldo.set_option(ldap.OPT_REFERRALS, 0)
    try:
        ldo.simple_bind_s("%s@%s" % (user, app.config['LDAP_DOMAIN']), secret)

        filter_str = '(sAMAccountName=%s)' % ldap.filter.escape_filter_chars(user)
        ret = ldo.search_s(app.config['LDAP_BASE'], ldap.SCOPE_SUBTREE,
                           filterstr=filter_str, attrlist=['memberOf'])
        user_attrs = [x for x in ret if x[0] is not None][0][1]
        user_groups = ldap_user_get_groups(user_attrs)
        return AuthDetails(username=user, groups=user_groups)
    except ldap.INVALID_CREDENTIALS:
        return AUTH_FAILED


def auth(user, secret):
    if app.config['BACKDOOR_AUTH']:
        if secret == 'woot' and user == 'woot':
            return AuthDetails(username='woot', groups=[])

    return ldap_login(user, secret)


def parse_csr(csr, encoding):
    if encoding != 'pem':
        return None

    return M2Crypto.X509.load_request_string(csr.encode('ascii'))


def validate_csr(auth_result, csr):
    args = {'auth_result': auth_result, 'csr': csr, 'app': app}
    for validator in app.config['VALIDATORS']:
        if not isinstance(validator, tuple):
            raise Exception("Validator should be defined by a tuple (got '%s' instead)" % (validator,))
        elif len(validator) == 1:
            validator_name, params = validator[0], {}
        elif len(validator) == 2:
            validator_name, params = validator
        elif len(validator) > 2:
            raise Exception("Validator config incorrect: '%s'" % (validator,))

        if not hasattr(validators, validator_name):
            raise Exception("Could not find validator named '%s'" % (validator,))
        new_kwargs = args.copy()
        new_kwargs.update(params)
        getattr(validators, validator_name)(**new_kwargs)


def sign(csr):
    ca = M2Crypto.X509.load_cert(app.config["CA_CERT"])
    key = M2Crypto.EVP.load_key(app.config["CA_KEY"])

    new_cert = M2Crypto.X509.X509()
    new_cert.set_version(0)

    now = int(time.time())
    start_time = M2Crypto.ASN1.ASN1_UTCTIME()
    start_time.set_time(now)
    end_time = M2Crypto.ASN1.ASN1_UTCTIME()
    end_time.set_time(now+(app.config['VALID_HOURS']*60*60))

    new_cert.set_not_before(start_time)
    new_cert.set_not_after(end_time)

    new_cert.set_pubkey(pkey=csr.get_pubkey())
    new_cert.set_subject(csr.get_subject())
    new_cert.set_issuer(ca.get_subject())
    new_cert.set_serial_number(int(uuid.uuid4().get_hex(), 16))

    new_cert.sign(key, app.config['SIGNING_HASH'])

    new_cert.save(os.path.join(
        app.config['CERTS_DIRECTORY'],
        '%s.crt' % new_cert.get_fingerprint(app.config['SIGNING_HASH'])))

    return new_cert.as_pem()


@app.route("/")
def fail():
    return 'Nothing to see here\n', 404


@app.route("/robots.txt")
def robots():
    txt = "User-agent: *\nDisallow: /\n"
    return txt, 200


@app.route("/sign", methods=['POST'])
def sign_request():
    """
    for key in ('user','authtype','csr','encoding','secret'):
        if not request.args.has_key(key):
            print '%s key missing from request:' % key
            print request.args.keys()
            return 'Request is missing keys!\n', 500

    """
    auth_result = auth(request.form['user'], request.form['secret'])
    if auth_result is AUTH_FAILED:
        return 'Authentication Failure\n', 403

    csr = parse_csr(request.form['csr'], request.form['encoding'])
    if csr is None:
        return 'CSR cannot be parsed\n', 400

    try:
        validate_csr(auth_result, csr)
    except validators.ValidationError as e:
        return 'Validation failed: %s\n' % e, 409

    cert = sign(csr)
    if not cert:
        return 'Signing Failure\n', 500

    # TODO: Probably need some nice headers or some other schtuff
    return cert, 200


def run_server():
    app.run(
        host=app.config['BIND_HOST'],
        port=app.config['BIND_PORT'])

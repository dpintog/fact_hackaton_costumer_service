"""Stable demo passwords derived from a private vault seed."""

import base64
import hashlib
import hmac


def scenario_password(seed, username):
    if not isinstance(seed, str) or len(seed) < 32:
        raise ValueError("The deployment credential seed is missing or too short.")
    value = hmac.new(seed.encode(), username.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def build_access_users(document, catalog):
    users = [dict(user) for user in document["users"] if user["role"] == "operator"]
    if not users:
        raise ValueError("The vault credential document needs an operator.")
    for profile in catalog["profiles"]:
        users.append(dict(username=profile["username"],
                          password=scenario_password(document["scenario_password_seed"], profile["username"]),
                          customer_id=profile["customer_id"], role="customer", scenario=profile["scenario"]))
    return dict(scope="cloud_demo_access", users=users)

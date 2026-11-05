# Responsibility: Verify the key format is the one the plan documents, and that only a constant-time compare accepts a secret.
from __future__ import annotations

import hmac
from hashlib import sha256
from unittest.mock import patch

from meshpipeline.contracts import api_key


def test_a_minted_key_is_presented_in_the_documented_form():
    minted = api_key.mint()
    assert minted.presented.startswith("hx_live_"), minted.presented
    assert minted.presented == f"{minted.key_prefix}_{minted.secret}"


def test_the_stored_hash_is_the_sha256_of_the_secret_and_nothing_else():
    minted = api_key.mint()
    assert minted.key_hash == sha256(minted.secret.encode()).hexdigest()
    # The stored form must not carry the secret in any recoverable way.
    assert minted.secret not in minted.key_hash
    assert minted.secret not in minted.key_prefix


def test_the_secret_carries_the_declared_entropy():
    # secrets.token_urlsafe(32) is 32 bytes base64url-encoded - 43 characters, no padding.
    assert api_key.SECRET_ENTROPY_BYTES == 32
    assert len(api_key.mint().secret) >= 43


def test_two_mints_share_neither_prefix_nor_secret():
    a, b = api_key.mint(), api_key.mint()
    assert a.key_prefix != b.key_prefix
    assert a.secret != b.secret


def test_the_public_identifier_never_contains_the_separator():
    # The presented key is split on the FIRST separator after `hx_live_`; an identifier carrying
    # one would silently move bytes from the identifier into the secret.
    for _ in range(50):
        identifier = api_key.mint().key_prefix[len("hx_live_"):]
        assert identifier and "_" not in identifier


def test_a_minted_key_parses_back_to_its_prefix_and_secret():
    minted = api_key.mint()
    parsed = api_key.parse(minted.presented)
    assert parsed is not None
    assert parsed.key_prefix == minted.key_prefix
    assert parsed.secret == minted.secret


def test_a_secret_containing_the_separator_still_parses_whole():
    presented = "hx_live_abc123_secret_with_underscores"
    parsed = api_key.parse(presented)
    assert parsed is not None
    assert parsed.key_prefix == "hx_live_abc123"
    assert parsed.secret == "secret_with_underscores"


def test_a_credential_that_is_not_a_live_key_does_not_parse():
    for presented in (None, "", "   ", "hx_live_", "hx_live_abc", "hx_live_abc_",
                      "hx_test_abc_secret", "sk-abc", "Bearer hx_live_a_b", "hx_live__secret"):
        assert api_key.parse(presented) is None, presented


def test_a_secret_matches_only_its_own_hash():
    minted = api_key.mint()
    assert api_key.secret_matches(minted.secret, minted.key_hash)
    assert not api_key.secret_matches(minted.secret + "x", minted.key_hash)
    assert not api_key.secret_matches("", minted.key_hash)
    assert not api_key.secret_matches(minted.secret, api_key.mint().key_hash)


def test_the_comparison_is_constant_time():
    minted = api_key.mint()
    with patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
        assert api_key.secret_matches(minted.secret, minted.key_hash)
    assert compare.called, "the hash comparison does not go through hmac.compare_digest"


def test_the_decoy_hash_matches_no_real_secret():
    # The decoy exists so an unknown prefix costs the same comparison a known one does.
    for _ in range(10):
        assert not api_key.secret_matches(api_key.mint().secret, api_key.DECOY_HASH)

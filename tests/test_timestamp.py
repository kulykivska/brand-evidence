from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

import httpx
import pytest
import respx
from asn1crypto import algos, cms, core, tsp

from brand_evidence.capture.timestamp import Rfc3161Authority, parse_response

TSA = "https://tsa.example/tsr"


def fake_tsr(
    digest: bytes, nonce: int | None, *, status: str = "granted", when: datetime | None = None
) -> bytes:
    imprint = tsp.MessageImprint(
        {
            "hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
            "hashed_message": digest,
        }
    )
    info = tsp.TSTInfo(
        {
            "version": "v1",
            "policy": "1.3.6.1.4.1.13762.3",
            "message_imprint": imprint,
            "serial_number": 42,
            "gen_time": core.GeneralizedTime(when or datetime(2026, 9, 15, 4, 0, tzinfo=UTC)),
            **({"nonce": nonce} if nonce is not None else {}),
        }
    )
    token = cms.ContentInfo(
        {
            "content_type": "signed_data",
            "content": cms.SignedData(
                {
                    "version": "v3",
                    "digest_algorithms": [algos.DigestAlgorithm({"algorithm": "sha256"})],
                    "encap_content_info": cms.EncapsulatedContentInfo(
                        {
                            "content_type": "tst_info",
                            "content": core.ParsableOctetString(info.dump()),
                        }
                    ),
                    "signer_infos": [],
                }
            ),
        }
    )
    # asn1crypto insists on the token field even for a rejection; the parser checks status first.
    return tsp.TimeStampResp(
        {
            "status": tsp.PKIStatusInfo({"status": status}),
            "time_stamp_token": token,
        }
    ).dump()


class EchoTSA:
    """Answers each request with a token for exactly the digest and nonce it received."""

    def __init__(self, status: str = "granted", tamper: bool = False) -> None:
        self.status, self.tamper, self.requests = status, tamper, []  # type: ignore[var-annotated]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        req = tsp.TimeStampReq.load(request.content)
        self.requests.append(req)
        digest = req["message_imprint"]["hashed_message"].native
        if self.tamper:
            digest = os.urandom(32)
        return httpx.Response(
            200,
            content=fake_tsr(digest, req["nonce"].native, status=self.status),
            headers={"Content-Type": "application/timestamp-reply"},
        )


@respx.mock
def test_stamp_sends_only_the_digest_and_returns_tsa_time() -> None:
    echo = EchoTSA()
    respx.post(TSA).mock(side_effect=echo)
    token = Rfc3161Authority(TSA, httpx.Client()).stamp(b"private manifest contents")
    sent = echo.requests[0]
    assert (
        sent["message_imprint"]["hashed_message"].native
        == hashlib.sha256(b"private manifest contents").digest()
    )
    assert b"private manifest" not in sent.dump()
    assert sent["cert_req"].native is True
    assert token.gen_time.startswith("2026-09-15T04:00:00") and token.serial == "42"
    assert token.digest_hex == hashlib.sha256(b"private manifest contents").hexdigest()


@respx.mock
def test_rejected_status_raises() -> None:
    respx.post(TSA).mock(side_effect=EchoTSA(status="rejection"))
    with pytest.raises(RuntimeError, match="TSA refused"):
        Rfc3161Authority(TSA, httpx.Client()).stamp(b"x")


@respx.mock
def test_digest_mismatch_raises() -> None:
    respx.post(TSA).mock(side_effect=EchoTSA(tamper=True))
    with pytest.raises(RuntimeError, match="digest does not match"):
        Rfc3161Authority(TSA, httpx.Client()).stamp(b"x")


def test_nonce_mismatch_raises() -> None:
    digest = hashlib.sha256(b"x").digest()
    with pytest.raises(RuntimeError, match="nonce"):
        parse_response(fake_tsr(digest, 1), digest, 2, TSA)

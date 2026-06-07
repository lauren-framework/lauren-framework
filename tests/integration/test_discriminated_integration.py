"""Integration tests: Discriminated wired into extractor and streaming."""

import dataclasses
from typing import Literal

import pytest

from lauren import Discriminated


@dataclasses.dataclass
class PaymentCard:
    method: Literal["card"] = "card"
    last4: str = ""


@dataclasses.dataclass
class PaymentCash:
    method: Literal["cash"] = "cash"
    amount: float = 0.0


PaymentType = Discriminated[PaymentCard | PaymentCash, "method"]


class TestDiscriminatedWithExtractorPipeline:
    def test_extractor_dispatches_card(self):
        from lauren.extractors import _validate_json

        result = _validate_json({"method": "card", "last4": "1234"}, PaymentType, "body")
        assert isinstance(result, PaymentCard)
        assert result.last4 == "1234"

    def test_extractor_dispatches_cash(self):
        from lauren.extractors import _validate_json

        result = _validate_json({"method": "cash", "amount": 50.0}, PaymentType, "body")
        assert isinstance(result, PaymentCash)
        assert result.amount == 50.0

    def test_extractor_unknown_tag_raises_extractor_error(self):
        from lauren.extractors import _validate_json
        from lauren.exceptions import ExtractorError

        with pytest.raises(ExtractorError) as exc_info:
            _validate_json({"method": "crypto"}, PaymentType, "body")
        assert "crypto" in str(exc_info.value.detail["errors"])

    def test_extractor_missing_tag_raises_extractor_error(self):
        from lauren.extractors import _validate_json
        from lauren.exceptions import ExtractorError

        with pytest.raises(ExtractorError):
            _validate_json({"last4": "9999"}, PaymentType, "body")


class TestDiscriminatedWithStreamingAdapter:
    def setup_method(self):
        from lauren.streaming import _ADAPTER_CACHE

        _ADAPTER_CACHE.clear()

    def test_build_adapter_accepts_discriminated_type(self):
        from lauren.streaming import _build_adapter

        adapter = _build_adapter(PaymentType)
        assert adapter is not None

    def test_adapter_validates_correctly(self):
        from lauren.streaming import _build_adapter

        adapter = _build_adapter(PaymentType)
        assert adapter is not None
        result = adapter.validate_python({"method": "card", "last4": "5678"})
        assert isinstance(result, PaymentCard)

    def test_adapter_dump_python_returns_dict(self):
        from lauren.streaming import _build_adapter

        adapter = _build_adapter(PaymentType)
        assert adapter is not None
        card = PaymentCard(last4="1234")
        dumped = adapter.dump_python(card)
        assert isinstance(dumped, dict)
        assert dumped["last4"] == "1234"

    def test_pydantic_discriminated_still_works_when_installed(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel, Field
        from typing import Annotated, Union
        from lauren.streaming import _build_adapter, _ADAPTER_CACHE

        class PCard(BaseModel):
            method: Literal["card"] = "card"
            last4: str = ""

        class PCash(BaseModel):
            method: Literal["cash"] = "cash"

        PPayment = Annotated[Union[PCard, PCash], Field(discriminator="method")]
        _ADAPTER_CACHE.clear()
        adapter = _build_adapter(PPayment)
        assert adapter is not None
        result = adapter.validate_python({"method": "card", "last4": "0000"})
        assert isinstance(result, PCard)

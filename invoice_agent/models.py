"""Shared data models for the invoice pipeline.

``Invoice`` and ``LineItem`` are both Grok's strict extraction schema and the data code checks:
numbers are exact Decimals (shown to Grok as JSON numbers), text is copied as printed, and
``LineItem.sku`` is set by ``validate`` and hidden from Grok. The rest is what later stages produce.
"""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, WithJsonSchema
from pydantic.json_schema import SkipJsonSchema

Status = Literal["PAID", "REJECTED", "ESCALATED", "DUPLICATE", "ERROR"]
SCRUTINY_THRESHOLD = Decimal("10000")  # amounts above this need extra scrutiny

# Extraction fields are all required and nullable, as strict mode wants. Grok sees numbers as plain JSON numbers
# (Decimal's default schema adds a string with a pattern, which strict mode rejects); pydantic reads a JSON or
# float number through its text, so 0.1 becomes exactly Decimal("0.1").
Number = Annotated[Decimal, WithJsonSchema({"type": "number"})]


class LineItem(BaseModel):
    """One invoice line as printed."""

    item: str = Field(description="Product name as printed, without annotations such as a parenthetical remark.")
    quantity: Number | None = Field(description="Quantity as printed. Null if the line shows none.")
    unit_price: Number | None = Field(description="Price per unit as printed. Null if the line shows none.")
    line_total: Number | None = Field(description="The line's amount as printed. Null if not printed; never computed.")
    note: str | None = Field(description="Annotation printed for this line, such as a parenthetical. Null if none.")
    sku: SkipJsonSchema[str | None] = None  # catalog SKU set by validate(); None when the item is unknown


class Invoice(BaseModel):
    """An invoice's fields copied from the document. Never computed, corrected or guessed; null when not printed."""

    invoice_number: str | None = Field(description="Invoice number as printed, without its label.")
    revision: str | None = Field(description="Revision marker if the document states one as a value. Null otherwise.")
    vendor_name: str | None = Field(description="Name of the seller asking to be paid (never the customer).")
    vendor_address: str | None = Field(description="The seller's address as printed. Null if absent.")
    invoice_date: str | None = Field(description="Issue date exactly as printed.")
    due_date: str | None = Field(description="Due date exactly as printed, even if it is not a real date.")
    currency: str | None = Field(
        description="ISO code stated, or implied by the symbol on the amounts (€ is EUR, $ is USD). Null if neither."
    )
    line_items: list[LineItem] = Field(description="Every line item, in document order, including repeated products.")
    subtotal: Number | None = Field(description="Subtotal as printed. Null if absent.")
    tax_rate: str | None = Field(description="Tax rate as printed, with its percent sign if any. Null if absent.")
    tax_amount: Number | None = Field(description="Tax amount as printed. Null if absent.")
    shipping: Number | None = Field(description="Shipping, freight or handling charge as printed. Null if absent.")
    total: Number | None = Field(description="The final amount the invoice asks to be paid, as printed.")
    payment_terms: str | None = Field(description="Payment terms as printed. Null if absent.")
    notes: str | None = Field(description="Other message text a reviewer should see, verbatim. Null if none.")


class Check(BaseModel):
    code: str  # a category from validate.CHECK_CODES when passed, else a failure code from validate.FAILURE_CATEGORY
    passed: bool
    message: str


class StockLine(BaseModel):
    sku: str
    requested: Decimal  # summed across every line with this SKU (positive quantities only)
    in_stock: int
    unit_price: Decimal | None = None  # catalog price


class ValidationReport(BaseModel):
    checks: list[Check] = Field(default_factory=list)
    stock: list[StockLine] = Field(default_factory=list)  # one per catalog SKU on the invoice, in first-seen order
    amount: Decimal | None = None  # amount to pay: the stated total in whole cents; None when no total is stated
    requires_scrutiny: bool = False  # amount > SCRUTINY_THRESHOLD

    @property
    def failures(self) -> list[Check]:
        """Every failed check; any one of them rejects the invoice."""
        return [c for c in self.checks if not c.passed]


class Decision(BaseModel):
    """The VP agent's decision."""

    action: Literal["approve", "reject", "escalate"] = Field(
        description="approve = pay it; reject = do not pay; escalate = a human must review."
    )
    rationale: str = Field(description="2-4 sentences explaining the decision, citing specific facts from the invoice.")
    concerns: list[str] = Field(description="Specific concerns or red flags noticed, one per entry. Empty if none.")


class Critique(BaseModel):
    """The critic's review of one VP decision."""

    verdict: Literal["accept", "revise"] = Field(
        description="accept = the decision follows policy and every claim in it is supported by the input; "
        "revise = the VP must reconsider."
    )
    problems: list[str] = Field(
        description="Each specific problem with the decision, citing the evidence, one per entry. Empty when accepting."
    )


class ToolCall(BaseModel):
    """One lookup the VP agent made through a tool."""

    round: int  # VP round (1-based) that made the call
    name: str
    args: dict[str, Any]
    result: Any  # JSON-able tool output exactly as returned to the model


class RunState(BaseModel):
    run_id: str
    invoice_path: str
    source_text: str | None = None
    invoice: Invoice | None = None  # as extracted; after validation, the copy with skus and currency set
    validation: ValidationReport | None = None
    vp_decisions: list[Decision] = Field(default_factory=list)  # every VP decision, in order
    critiques: list[Critique] = Field(default_factory=list)  # critiques[i] reviews vp_decisions[i]
    tool_calls: list[ToolCall] = Field(default_factory=list)  # every VP lookup, in order
    extract_attempts: int = 0  # attempts that got an answer: 1 + retries (an LLM error ends the run uncounted)
    extract_problems: list[str] = Field(default_factory=list)  # why the last extraction was rejected; fed back
    route: Literal["pay", "reject", "escalate"] | None = None  # set by the guardrail
    overridden: bool = False  # the guardrail overrode the VP (a failed check, or a critique left unresolved)
    status: Status | None = None
    reasons: list[str] = Field(default_factory=list)
    payment_ref: str | None = None
    error: str | None = None
    llm_calls: list[dict] = Field(default_factory=list)  # one meta dict per LLM call, in order
    timings_ms: dict[str, float] = Field(default_factory=dict)  # summed per node

    @property
    def decision(self) -> Decision | None:
        """The VP's latest decision."""
        return self.vp_decisions[-1] if self.vp_decisions else None

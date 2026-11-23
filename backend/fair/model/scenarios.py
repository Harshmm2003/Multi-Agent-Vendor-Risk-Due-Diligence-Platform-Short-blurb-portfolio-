"""
The loss scenarios the model knows, and which FAIR loss forms each one uses.

A template does not hold numbers. It tells the agent (and a person writing a
scenario file) what the scenario means and how to fill in its factors.
"""

from dataclasses import dataclass

from fair.model.taxonomy import PrimaryForm, Scenario, SecondaryForm


@dataclass(frozen=True)
class ScenarioTemplate:
    scenario: Scenario
    title: str
    description: str
    # A one-shot event happens at most once a year (a vendor can only go
    # bankrupt once), so the simulation caps its event count at 1.
    one_shot: bool
    primary_forms: tuple[PrimaryForm, ...]
    secondary_forms: tuple[SecondaryForm, ...]
    guidance: str


_ALL_SECONDARY = tuple(SecondaryForm)

TEMPLATES: dict[Scenario, ScenarioTemplate] = {
    Scenario.VENDOR_BREACH: ScenarioTemplate(
        scenario=Scenario.VENDOR_BREACH,
        title="Vendor data breach",
        description="Data the client shares with the vendor is exposed through the vendor.",
        one_shot=False,
        primary_forms=(
            PrimaryForm.PRODUCTIVITY,
            PrimaryForm.RESPONSE,
            PrimaryForm.REPLACEMENT,
        ),
        secondary_forms=_ALL_SECONDARY,
        guidance=(
            "threat_event_frequency: serious attack attempts on the vendor per year. "
            "vulnerability: chance an attempt succeeds; lower with verified SOC 2 Type 2 / "
            "ISO 27001, higher with a breach history. Magnitudes scale with the number and "
            "sensitivity of records shared; fines depend on the data type and jurisdiction."
        ),
    ),
    Scenario.VENDOR_INSOLVENCY: ScenarioTemplate(
        scenario=Scenario.VENDOR_INSOLVENCY,
        title="Vendor insolvency",
        description="The vendor fails financially and stops delivering the service.",
        one_shot=True,
        primary_forms=(PrimaryForm.PRODUCTIVITY, PrimaryForm.REPLACEMENT),
        secondary_forms=(),
        guidance=(
            "threat_event_frequency: the vendor's annual probability of default (from the "
            "Altman Z band when available). vulnerability: 1.0, since every default is a "
            "loss event. replacement: switching and migration cost plus any prepaid fees "
            "lost. productivity: disruption while switching."
        ),
    ),
    Scenario.VENDOR_OUTAGE: ScenarioTemplate(
        scenario=Scenario.VENDOR_OUTAGE,
        title="Vendor service outage",
        description="The vendor's service is unavailable long enough to hurt the business.",
        one_shot=False,
        primary_forms=(PrimaryForm.PRODUCTIVITY, PrimaryForm.RESPONSE),
        secondary_forms=(SecondaryForm.REPUTATION, SecondaryForm.COMPETITIVE_ADVANTAGE),
        guidance=(
            "threat_event_frequency: significant outages per year. vulnerability: chance an "
            "outage outlasts the client's tolerance (lower if the client has a fallback). "
            "productivity: revenue or cost at risk per hour x expected outage hours."
        ),
    ),
    Scenario.SANCTIONS_EXIT: ScenarioTemplate(
        scenario=Scenario.SANCTIONS_EXIT,
        title="Forced exit for sanctions",
        description="The vendor becomes sanctioned and the client must stop using it at once.",
        one_shot=True,
        primary_forms=(PrimaryForm.PRODUCTIVITY, PrimaryForm.REPLACEMENT),
        secondary_forms=(SecondaryForm.FINES_JUDGMENTS, SecondaryForm.REPUTATION),
        guidance=(
            "threat_event_frequency: annual chance the vendor is designated. vulnerability: "
            "1.0. fines_judgments: penalties if payments continue after designation."
        ),
    ),
}

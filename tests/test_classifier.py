from __future__ import annotations

from file_control.models import (
    Campaign,
    CampaignOrganization,
    Dimension,
    DimensionValue,
    Organization,
    ParseStatus,
    StructureMode,
)
from file_control.services.classifier import classify, normalize_path, relative_to_campaign


def campaign_graph() -> Campaign:
    organization = Organization(
        id=1,
        code="ORG-1",
        name="ООО Ромашка",
        folder_name="ООО Ромашка",
        active=True,
    )
    campaign = Campaign(
        id=1,
        name="Квартальный сбор",
        year=2026,
        root_path="/Сбор/2026",
        storage_user_id="collector",
        created_by="admin",
        structure_mode=StructureMode.STRICT,
    )
    campaign.organizations = [CampaignOrganization(id=1, campaign=campaign, organization=organization, included=True)]
    period = Dimension(id=10, campaign=campaign, position=0, key="period", name="Период", fixed=True)
    period.values = [DimensionValue(id=100, dimension=period, value="1 квартал", display_name="1 квартал", active=True)]
    campaign.dimensions = [period]
    return campaign


def test_path_normalization_and_boundary() -> None:
    assert normalize_path(r"Сбор\2026//file.xlsx") == "/Сбор/2026/file.xlsx"
    assert relative_to_campaign("/Сбор/2026/a.txt", "/Сбор/2026") == "a.txt"
    assert relative_to_campaign("/Сбор/20260/a.txt", "/Сбор/2026") is None


def test_strict_classification() -> None:
    result = classify(campaign_graph(), "/Сбор/2026/ООО Ромашка/1 квартал/report.xlsx")
    assert result.status == ParseStatus.OK
    assert result.organization.code == "ORG-1"
    assert result.dimensions[0].dimension_value_id == 100


def test_unknown_values_are_reported() -> None:
    unknown_org = classify(campaign_graph(), "/Сбор/2026/Другая компания/1 квартал/report.xlsx")
    assert unknown_org.status == ParseStatus.UNKNOWN_ORGANIZATION

    unknown_period = classify(campaign_graph(), "/Сбор/2026/ООО Ромашка/5 квартал/report.xlsx")
    assert unknown_period.status == ParseStatus.UNKNOWN_DIMENSION
    assert "5 квартал" in unknown_period.message

"""УДД и УУР по Приложению А2 приказа № 103н."""
from __future__ import annotations

# Приложение А2: УДД по дизайну исследования (study_design_enum) для шкалы диагностики и шкалы лечения
UDD_DIAGNOSTIC = {
    "meta_analysis_diagnostic": 1, "meta_analysis_rct": 1, "systematic_review_rct": 2,
    "diagnostic_with_reference": 2, "rct": 2, "diagnostic_without_reference": 3, "cohort": 3, "case_control": 3,
    "non_comparative": 4, "case_series": 4, "case_report": 4, "expert_opinion": 5,
}
UDD_TREATMENT = {
    "meta_analysis_rct": 1, "systematic_review_rct": 2, "rct": 2, "cohort": 3,
    "case_control": 4, "non_comparative": 4, "case_series": 4, "case_report": 4, "expert_opinion": 5,
}
UDD_RATIONALE = {
    1: "систематический обзор РКИ или исследований с референсным методом с метаанализом",
    2: "отдельные РКИ или исследования с референсным методом, систематические обзоры",
    3: "нерандомизированные сравнительные исследования, в том числе когортные",
    4: "несравнительные исследования, серии и описания случаев, «случай-контроль»",
    5: "мнение экспертов или обоснование механизма действия",
}
SECTION_TYPE = {"2": "diagnostic", "3": "therapeutic", "4": "rehab", "5": "preventive"}
STAGE = {"diagnostic": "diagnosis", "therapeutic": "treatment", "rehab": "rehabilitation", "preventive": "prevention"}


def intervention_type_for(section: str) -> str:
    return SECTION_TYPE.get(section.split(".")[0], "therapeutic")


def scale_for(itype: str) -> str:
    # правило онтологии: diagnostic → udd_diagnostic, остальные типы → udd_treatment
    return "udd_diagnostic" if itype == "diagnostic" else "udd_treatment"


def udd_for(design: str | None, section: str) -> int | None:
    scale = scale_for(intervention_type_for(section))
    return (UDD_DIAGNOSTIC if scale == "udd_diagnostic" else UDD_TREATMENT).get(design or "")


def preliminary_uur(udds: list[int], conflicting: bool) -> tuple[str | None, str]:
    """УУР по табл. 3 Приложения А2; udds — по одному значению на независимый источник."""
    if not udds:
        return None, "УДД источников не определён"
    best = min(udds)
    if best <= 2 and not conflicting and max(udds) <= 2 and (best == 1 or len(udds) >= 2):
        return "A", "все источники УДД 1–2, выводы согласованы"
    if best <= 2 and not conflicting and len(udds) == 1:
        return "B", "единственное исследование высокого уровня"
    if best <= 2:
        return "B", "есть противоречащие доказательства" if conflicting else "не все источники высокого качества"
    if best == 3 and not conflicting:
        return "B", "нерандомизированные сравнительные исследования, выводы согласованы"
    return "C", "доказательства низкого уровня или несогласованные"


def strength_phrase(direction: str, uur: str | None) -> str:
    """Выводится из пары (direction, uur), как требует онтология."""
    strong = uur in ("A", "B")
    if direction == "negative":
        return "Не рекомендуется" if strong else "Не рекомендуется рассматривать"
    return "Рекомендуется" if strong else "Рекомендуется рассмотреть возможность"

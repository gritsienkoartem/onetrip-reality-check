"""Уточнение места до сборки плана.

Геокодер может найти несколько населённых пунктов с одним названием
(например, «Ростов»). В этом случае пользователю задаётся один вопрос
с вариантами, а не угадывается город. Всё остальное решается без
вопросов: тип объекта (город, озеро, достопримечательность) сохраняется
в GeoPoint.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from models import TripRequest
from providers.geo import GeoPoint, PlaceResolution

PLACE_FIELDS = ("destination_city", "origin_city")

FIELD_LABELS_RU = {
    "destination_city": "Куда едем",
    "origin_city": "Откуда выезжаем",
}


class PlaceResolver(Protocol):
    """Минимальный интерфейс геокодера, нужный планировщику."""

    def resolve(self, query: str) -> PlaceResolution: ...


@dataclass(frozen=True)
class AmbiguousPlace:
    """Название подходит к нескольким местам — нужен один ответ пользователя."""

    field: str
    query: str
    question: str
    candidates: list[GeoPoint]

    @property
    def field_label(self) -> str:
        return FIELD_LABELS_RU.get(self.field, "Место")

    @property
    def options(self) -> list[str]:
        """Подписи вариантов; повторы нумеруются, чтобы выбор был однозначным."""
        labels: list[str] = []
        for point in self.candidates:
            label = point.label_ru
            if label in labels:
                label = f"{label} ({len(labels) + 1})"
            labels.append(label)
        return labels


def resolve_request_places(
    request: TripRequest,
    resolver: PlaceResolver,
    *,
    skip_fields: Collection[str] = (),
) -> tuple[TripRequest, AmbiguousPlace | None]:
    """Проверяет города запроса и возвращает уточнённый запрос.

    Сначала проверяется пункт назначения, затем город выезда: пользователь
    отвечает только на один вопрос за раз. Если имя неоднозначно, запрос
    не меняется — вызывающая сторона показывает вопрос.
    """
    updated = request
    for field in PLACE_FIELDS:
        if field in skip_fields:
            continue
        query = getattr(updated, field)
        if not query:
            continue

        resolution = resolver.resolve(query)
        if resolution.ambiguous:
            return updated, AmbiguousPlace(
                field=field,
                query=query,
                question=resolution.question,
                candidates=resolution.matching_settlements,
            )

        selected = resolution.selected
        if (
            selected is not None
            and selected.is_settlement
            and selected.city
            and selected.city != query
        ):
            updated = updated.model_copy(update={field: selected.city})

    return updated, None

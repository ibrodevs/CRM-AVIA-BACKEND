from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

pytestmark = pytest.mark.django_db

SAMPLES_ROOT = Path(__file__).resolve().parents[3] / "CRM-Admin" / "квитанции"


@pytest.mark.parametrize(
    ("folder", "name", "kind", "passenger", "total", "route_from", "route_to", "date"),
    [
        (
            "Авиа",
            "Aleksandr_Zaliubin_421_2124922516_8202068.pdf",
            "avia",
            "ZALIUBIN ALEKSANDR MIKHAILOVICH",
            "54643.00",
            "Москва, Домодедово",
            "Чита",
            "17.06.2025",
        ),
        (
            "Авиа",
            "Galina_Sivaks_1111484695_8302818.pdf",
            "avia",
            "SIVAKS GALINA",
            "2897.00",
            "РИМ, ФЬЮМИЧИНО/ЛЕОНАРДО ДА ВИНЧИ",
            "HAHN, ХАН",
            "29.07.2025",
        ),
        (
            "Авиа",
            "Nataliia_Popovich_235_3497052386_8325212.pdf",
            "avia",
            "POPOVICH NATALIIA",
            "20982.63",
            "ЗАГРЕБ, ПЛЕСО",
            "СТАМБУЛА",
            "22.06.2025",
        ),
        (
            "Авиа",
            "Сергей_Вадимович_Богацков_425_6123628210_8309085.pdf",
            "avia",
            "БОГАЦКОВ СЕРГЕЙ В",
            "10099.00",
            "Саратов",
            "Сочи",
            "05.06.2025",
        ),
        (
            "ЖД",
            "ЖД - Антонов Вадим Сергеевич (1).pdf",
            "rail",
            "АНТОНОВ ВАДИМ СЕРГЕЕВИЧ",
            "1268.10",
            "Москва Казанская",
            "Рязань 2",
            "27.05.2025",
        ),
        (
            "ЖД",
            "ЖД - Антонов Вадим Сергеевич.pdf",
            "rail",
            "АНТОНОВ В. С.",
            "875.00",
            "РЯЗАНЬ 1",
            "МОСКВА КАЗАНСКАЯ",
            "27.05.2025",
        ),
        (
            "ЖД",
            "ЖД - Кутуков Сергей Алексеевич.pdf",
            "rail",
            "КУТУКОВ С. А.",
            "3324.90",
            "МОСКВА КАЗАНСКАЯ",
            "РЯЗАНЬ 2",
            "27.05.2025",
        ),
        (
            "Отель",
            "gel'man_mixail_vladimirovich_2025-06-02.pdf",
            "hotel",
            "Гельман Михаил Владимирович",
            None,
            "",
            "Скай Порт",
            "02.06.2025",
        ),
        (
            "Отель",
            "voucher-ru-328343646.pdf",
            "hotel",
            "Fakhretdinov Ravilievich Maksim",
            None,
            "",
            "Гостиница Арена",
            "15.06.2025",
        ),
        (
            "Отель",
            "voucher-ru-945135381.pdf",
            "hotel",
            "Koloskov Evgenii, Koloskova Iuliia",
            None,
            "",
            "Hotel Eclat Beijing",
            "20.05.2025",
        ),
        (
            "Трансфер",
            "Ваучер трансфера.pdf",
            "transfer",
            "Сорокина Ольга",
            "4200.00",
            "Аэропорт Храброво (Калининград)",
            "Калининград",
            "22.05.2025",
        ),
    ],
)
def test_every_client_pdf_is_recognized_on_receipt_import_api(
    admin_client,
    folder,
    name,
    kind,
    passenger,
    total,
    route_from,
    route_to,
    date,
):
    path = SAMPLES_ROOT / folder / name
    response = admin_client.post(
        "/api/v1/receipt-imports/",
        {"file": SimpleUploadedFile(name, path.read_bytes(), content_type="application/pdf")},
        format="multipart",
    )
    assert response.status_code == 201, response.content

    result = admin_client.get(f"/api/v1/receipt-imports/{response.json()['id']}/result/")
    assert result.status_code == 200, result.content
    body = result.json()
    assert body["parser_status"] == "parsed"
    assert body["extracted"]["service_kind"] == kind
    assert body["draft"]["passenger_name"] == passenger
    assert body["draft"]["total"] == total
    assert body["draft"]["segments"]
    assert body["draft"]["segments"][0]["from"] == route_from
    assert body["draft"]["segments"][0]["to"] == route_to
    assert body["draft"]["segments"][0]["date"] == date

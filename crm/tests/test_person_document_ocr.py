import pytest

from crm.person_document_ocr import parse_cyrillic_document, parse_mrz, recognize_person_document

pytestmark = pytest.mark.django_db

# Эталонные MRZ из ICAO 9303: контрольные цифры в них корректны.
TD3 = (
    "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
    "L898902C36UTO7408122F1204159ZE184226B<<<<<10"
)
TD1 = (
    "I<UTOD231458907<<<<<<<<<<<<<<<\n"
    "7408122F1204159UTO<<<<<<<<<<<6\n"
    "ERIKSSON<<ANNA<MARIA<<<<<<<<<<"
)


class TestMrzParsing:
    def test_td3_passport(self):
        parsed = parse_mrz(TD3)
        assert parsed["format"] == "TD3"
        assert parsed["surname"] == "ERIKSSON"
        assert parsed["given_name"] == "ANNA"
        assert parsed["middle_name"] == "MARIA"
        assert parsed["number"] == "L898902C3"
        assert parsed["birth_date"] == "1974-08-12"
        assert parsed["expiry_date"] == "2012-04-15"
        assert parsed["sex"] == "F"
        assert all(parsed["checks"].values()), "контрольные цифры эталона должны сходиться"

    def test_td1_id_card(self):
        parsed = parse_mrz(TD1)
        assert parsed["format"] == "TD1"
        assert parsed["document_kind"] == "id_card"
        assert parsed["number"] == "D23145890"
        assert parsed["birth_date"] == "1974-08-12"

    def test_mrz_survives_surrounding_ocr_noise(self):
        noisy = f"ПАСПОРТ\nKYRGYZ REPUBLIC\n\n{TD3}\n\nподпись"
        assert parse_mrz(noisy)["number"] == "L898902C3"

    def test_no_mrz_returns_none(self):
        assert parse_mrz("обычный текст без машиночитаемой зоны") is None

    def test_birth_and_expiry_centuries_are_resolved(self):
        parsed = parse_mrz(
            "P<KGZSATYMKULOV<<ADILHAN<<<<<<<<<<<<<<<<<<<<\n"
            "AC12345671KGZ9003152M3005121<<<<<<<<<<<<<<02"
        )
        # Дата рождения — в прошлом, срок действия — в будущем.
        assert parsed["birth_date"] == "1990-03-15"
        assert parsed["expiry_date"] == "2030-05-12"


class TestCyrillicFallback:
    def test_reads_number_names_and_dates(self):
        text = (
            "ПАСПОРТ РОССИЙСКОЙ ФЕДЕРАЦИИ\n"
            "Иванов Пётр Сергеевич\n"
            "45 12 123456\n"
            "Дата рождения 10.05.1990\n"
            "Действителен до 20.08.2031\n"
        )
        parsed = parse_cyrillic_document(text)
        assert parsed["surname"] == "Иванов"
        assert parsed["given_name"] == "Пётр"
        assert parsed["birth_date"] == "1990-05-10"
        assert parsed["expiry_date"] == "2031-08-20"
        assert parsed["document_kind"] == "national_passport"

    def test_empty_text_gives_nothing(self):
        assert parse_cyrillic_document("") == {}


class TestRecognition:
    def test_mrz_recognition_is_high_confidence(self, monkeypatch):
        monkeypatch.setattr(
            "crm.person_document_ocr._document_text", lambda content, mime: (TD3, {})
        )
        result = recognize_person_document(b"x", mime="image/jpeg")
        assert result["status"] == "recognized"
        assert result["source"] == "mrz_td3"
        assert result["confidence"] >= 90
        assert result["fields"]["surname"] == "ERIKSSON"
        assert result["fields"]["document_label"] == "Загранпаспорт"
        assert result["fields"]["sex"] == "Женский"

    def test_unreadable_document_asks_for_manual_input(self, monkeypatch):
        monkeypatch.setattr("crm.person_document_ocr._document_text", lambda content, mime: ("", {}))
        result = recognize_person_document(b"x", mime="image/png")
        assert result["status"] == "manual_required"
        assert result["fields"] == {}


class TestRecognizeEndpoint:
    def test_requires_a_file(self, admin_client):
        response = admin_client.post("/api/v1/person-documents/recognize/", {}, format="multipart")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

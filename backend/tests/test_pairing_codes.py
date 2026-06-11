"""Генерация/нормализация pairing-кодов (без БД)."""
from app.services.pairing_service import ALPHABET, generate_code, hash_code, normalize_code


def test_code_format():
    code = generate_code()
    assert len(code) == 9 and code[4] == "-"
    assert all(ch in ALPHABET for ch in code.replace("-", ""))


def test_ambiguous_symbols_excluded():
    for ch in "01OIL":
        assert ch not in ALPHABET


def test_normalization_tolerant_to_user_input():
    """Пользователь диктует/вводит как угодно — хэш один и тот же."""
    assert normalize_code(" ab2-3cd4 ") == "AB23CD4"
    assert hash_code("AB23-CD45") == hash_code("ab23cd45") == hash_code(" ab2 3cd45 ")


def test_codes_are_unique_enough():
    assert len({generate_code() for _ in range(1000)}) == 1000

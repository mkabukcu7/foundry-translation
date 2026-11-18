import pytest

from services import contextual_translation, glossary

GLOSSARY = "it,en,es\npolizza danni,property policy,póliza de daños\nfranchigia,deductible,franquicia\n"


def test_static_glossary_selects_only_exact_document_terms():
    terms = glossary.relevant_terms(GLOSSARY, "it", "en", "La polizza danni è valida.", "static")
    assert [term["term"] for term in terms] == ["polizza danni"]


def test_retrieval_glossary_uses_local_sparse_term_vectors():
    terms = glossary.relevant_terms(GLOSSARY, "it", "en", "La polizza scade.", "retrieval")
    assert [term["term"] for term in terms] == ["polizza danni"]
    context = contextual_translation._context(
        [{"text": "La polizza scade."}], [0], terms, glossary_mode="retrieval",
    )
    assert context["terms"] == terms


def test_glossary_mode_none_and_unknown_modes():
    assert glossary.relevant_terms(GLOSSARY, "it", "en", "polizza", "none") == []
    with pytest.raises(ValueError, match="Glossary mode"):
        glossary.relevant_terms(GLOSSARY, "it", "en", "polizza", "vector")


def test_glossary_rejects_empty_or_injectable_terms():
    with pytest.raises(ValueError, match="empty"):
        glossary.relevant_terms("", "it", "en", "polizza", "static")
    malformed = 'it,en\n"polizza\npolicy",policy\n'
    with pytest.raises(ValueError, match="tabs or line breaks"):
        glossary.relevant_terms(malformed, "it", "en", "polizza policy", "retrieval")

from app.core.openapi import API_DESCRIPTION, OPENAPI_TAGS, OPERATION_DOCS
from app.main import app


def test_openapi_contains_api_metadata():
    schema = app.openapi()

    assert schema["info"]["title"] == "ProofGuard Audit API"
    assert schema["info"]["description"] == API_DESCRIPTION
    assert schema["tags"] == OPENAPI_TAGS


def test_every_endpoint_has_curated_summary_and_description():
    schema = app.openapi()
    operations = {
        (method.upper(), path): operation
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
    }

    assert set(operations) == set(OPERATION_DOCS)
    for key, operation in operations.items():
        expected_summary, expected_description = OPERATION_DOCS[key]
        assert operation["summary"] == expected_summary
        assert operation["description"] == expected_description


def test_swagger_and_redoc_are_enabled(client):
    swagger_response = client.get("/docs")
    redoc_response = client.get("/redoc")

    assert swagger_response.status_code == 200
    assert "Swagger UI" in swagger_response.text
    assert redoc_response.status_code == 200
    assert "ReDoc" in redoc_response.text

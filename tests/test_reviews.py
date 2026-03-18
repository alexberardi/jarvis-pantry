"""Tests for review endpoints."""


class TestListReviews:
    def test_not_found(self, client):
        resp = client.get("/v1/commands/nonexistent/reviews")
        assert resp.status_code == 404

    def test_empty_reviews(self, client, seed_data):
        resp = client.get("/v1/commands/get_stock_price/reviews")
        assert resp.status_code == 200
        assert resp.json()["reviews"] == []


class TestSubmitReview:
    def test_create_review(self, client, seed_data, db_session):
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        resp = client.post("/v1/commands/get_stock_price/reviews", json={
            "rating": 5,
            "title": "Great command!",
            "body": "Works perfectly.",
        })

        app.dependency_overrides.pop(validate_github_token, None)

        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

    def test_update_review(self, client, seed_data, db_session):
        from app.main import app
        from app.auth import validate_github_token
        from app.models import Review
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        # Create initial review
        review = Review(
            command_id=seed_data["command"].id,
            author_id=seed_data["author"].id,
            rating=3,
            title="OK",
            body="Meh",
        )
        db_session.add(review)
        db_session.commit()

        # Update it
        resp = client.post("/v1/commands/get_stock_price/reviews", json={
            "rating": 5,
            "title": "Updated!",
            "body": "Much better now.",
        })

        app.dependency_overrides.pop(validate_github_token, None)

        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

    def test_command_not_found(self, client, seed_data):
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        resp = client.post("/v1/commands/nonexistent/reviews", json={
            "rating": 5,
            "title": "test",
        })

        app.dependency_overrides.pop(validate_github_token, None)
        assert resp.status_code == 404

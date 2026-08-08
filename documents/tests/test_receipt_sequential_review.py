from documents.receipt_sequential_review_patch import (
    apply_review_state,
    receipt_item_review_status,
    receipt_review_progress,
)


def test_review_state_is_preserved_per_child_ticket():
    canonical = [
        {"ticketNo": "A1", "receiptIndex": 1},
        {"ticketNo": "A2", "receiptIndex": 2},
    ]
    source = [
        {
            "ticketNo": "A1",
            "reviewStatus": "reviewed",
            "reviewedAt": "2026-08-08T07:00:00Z",
            "reviewedBy": "operator-1",
        },
        {"ticketNo": "A2", "review_status": "pending"},
    ]

    result = apply_review_state(canonical, source)

    assert result[0]["reviewStatus"] == "reviewed"
    assert result[0]["reviewedAt"] == "2026-08-08T07:00:00Z"
    assert result[0]["reviewedBy"] == "operator-1"
    assert result[1]["reviewStatus"] == "pending"
    assert "reviewedAt" not in result[1]


def test_review_progress_points_to_first_unreviewed_ticket():
    items = [
        {"reviewStatus": "reviewed"},
        {"reviewStatus": "pending"},
        {"review_status": "reviewed"},
    ]

    progress = receipt_review_progress(items)

    assert progress == {
        "total": 3,
        "reviewed": 2,
        "complete": False,
        "next_index": 2,
    }


def test_review_progress_is_complete_only_when_every_ticket_is_reviewed():
    items = [
        {"reviewStatus": "reviewed"},
        {"reviewStatus": "checked"},
        {"reviewed": True},
    ]

    assert receipt_item_review_status(items[1]) == "reviewed"
    assert receipt_review_progress(items) == {
        "total": 3,
        "reviewed": 3,
        "complete": True,
        "next_index": None,
    }

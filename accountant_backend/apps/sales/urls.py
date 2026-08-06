from django.urls import path

from . import views

urlpatterns = [
    path("customers/<int:customer_id>/history/", views.CustomerHistoryView.as_view(), name="customer-history"),
    # Multi-customer period view backing the chat "View" button on a
    # report_view card — everyone who bought/paid something in a date range.
    path("entries/", views.EntriesInRangeView.as_view(), name="entries-in-range"),
    # Home tab's at-a-glance figures. Before this the Dashboard had nothing to
    # call and showed hardcoded sample numbers.
    path("dashboard/summary/", views.DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("sales/", views.RecordSaleView.as_view(), name="record-sale"),
    path("sales/<int:entry_id>/", views.EditSaleView.as_view(), name="edit-sale"),
    path("sales/<int:entry_id>/items/<int:index>/", views.DeleteSaleLineItemView.as_view(), name="delete-sale-line-item"),
    path("payments/", views.RecordPaymentView.as_view(), name="record-payment"),
    path("payments/<int:entry_id>/", views.EditPaymentView.as_view(), name="edit-payment"),
    path("entries/<int:entry_id>/", views.DeleteEntryView.as_view(), name="delete-entry"),
    path("actions/undo/<int:pending_undo_id>/", views.UndoActionView.as_view(), name="undo-action"),
]

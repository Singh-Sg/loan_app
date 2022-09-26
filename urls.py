from django.conf.urls import url
import loans.views as views

urlpatterns = [
    url(r'^collection-report/', views.CollectionSheetView.as_view(), name='collection-sheet'),
    url(r'^collection-report-pdf/', views.CollectionSheetPDFView.as_view(), name='collection-sheet-pdf'),
    url(r'^repayment-sheet/', views.repayment_sheet, name='repayment-sheet'),
    url(r'^register-payment/', views.register_payment, name='register-payment'),
    url(r'^collection-report2/', views.collection_report2, name='collection-report2'),
    url(r'^print-contract/(?P<pk>\d+)/$', views.PrintLoanContractView.as_view(), name='print-contract'),
    url(r'^print-contract/(?P<pk>\d+)/pdf/$', views.PrintLoanContractPDFView.as_view(), name='pdf-contract'),
    url(r'^contract/(?P<pk>\d+)/renew/$', views.create_renewal_contract, name='renew-contract'),
    url(r'^request-sheet/', views.request_sheet, name='request-sheet'),
    url(r'^disburse-sheet/', views.disburse_sheet, name='disburse-sheet'),
    url(r'^outstanding-loans/', views.outstanding_loans, name='outstanding-loans'),
    url(r'^late-loans/', views.late_loans, name='late-loans'),
    url(r'^signed-loan-requests-for-disbursement-sheet/', views.signed_loan_requests_for_disbursement_sheet,
        name='signed-loan-requests-for-disbursement-sheet'),
    url(r'^backend-today-view/', views.backend_today_view, name='backend-today-view'),
    url(r'^backend-new-loan-report/', views.backend_new_loan_reportview, name='backend-new-loan-report'),
    url(r'^reconciliation-high-level/', views.ReconciliationHighLevelView.as_view(), name='reconciliation-high-level'),
    url(r'^disbursement-report/$', views.disbursement_report, name='disbursement_report'),
    url(r'^customer-retention-report/$', views.customer_retention_report, name='customer_retention_report'),

]

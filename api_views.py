import logging
from datetime import date as d
from datetime import datetime, timedelta, timezone

from rest_framework import mixins, status, views, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from borrowers.models import Agent, Borrower
from borrowers.permissions import IsAgentOrStaff
from payments.models import (
    TRANSFER_METHOD_CASH_IN_HAND,
    TRANSFER_METHOD_PAY_WITH_WAVE,
    Transfer,
)

from .custom_serializers import RepaymentFullSerializer
from .models import (
    DISBURSEMENT_SENT,
    MANUAL_RECONCILED,
    NEED_MANUAL_RECONCILIATION,
    NOT_RECONCILED,
    Disbursement,
    Loan,
    Notification,
    ReasonForDelayedRepayment,
    Repayment,
    RepaymentScheduleLine,
    SuperUsertoLenderPayment,
)
from .permissions import DisbursePermissions, LoanViewPermissions, SuperUserOnlyView
from .serializers import (
    CashTransferSerializer,
    DisbursementDisburseSerializer,
    DisbursementSerializer,
    LoanPATCHSerializer,
    LoanPUTSerializer,
    LoanRequestReviewSerializer,
    LoanSerializer,
    LoanSerializerFullDetail,
    NewRepaymentSerializer,
    NoteSerializer,
    NotificationSerializer,
    PhotoSignatureSerializer,
    ReasonForDelayedRepaymentSerializer,
    ReconciliationPOSTSerializer,
    RepaymentScheduleLineSerializer,
    RepaymentSerializer,
    SuperUsertoLenderFullPaymentSerializer,
    SuperUsertoLenderPaymentCustomSerializer,
    SuperUsertoLenderPaymentSerializer,
)
from .signals import reconcile_with_intermediary


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 1000


class LoanViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows viewing and editing Loan objects
    """

    serializer_class = LoanSerializer
    permission_classes = (LoanViewPermissions,)
    # pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return None

        if user.is_staff:
            return Loan.objects.all()
        else:
            return Loan.objects.filter(borrower__agent__user=user).order_by(
                "-uploaded_at"
            )

    def get_serializer_class(self):
        """
        Select a different serializer depending on the request method used.
        This allows to customize the behaviour of the serializer and do some finer
        error handling.
        """
        if self.request.method in ("GET", "POST",):
            # reading: send the entire loan data, including repayments
            return LoanSerializer
        elif self.request.method in ("PATCH",):
            return LoanPATCHSerializer
        elif self.request.method in ("PUT",):
            return LoanPUTSerializer

    @action(detail=True, methods=["get"], url_path="full")
    def get_full_loan(self, request, pk=None):
        """
        Return the full detail of loan.
        """
        if not request.user.is_authenticated:
            raise NotAuthenticated()

        obj = Loan.objects.get(pk=pk)
        return Response(LoanSerializerFullDetail(obj).data)

    @action(detail=True, methods=["POST"], url_path="sign")
    def sign_loan(self, request, pk=None):
        """
        POST to this endpoint with a photo of the borrower to sign the loan request.
        This view will create a BaseBorrowerSignature (more exactly one of its child classes)
        instance, and save it, triggering the Loan.state change as appropriate.
        Expected format:
        {
            "loan": <loan_id>,
            "photo": <base64encoded_photo>
            "timestamp": "2013-01-29T12:34:56Z"
        }
        """
        data = request.data
        data["loan"] = pk
        serializer = PhotoSignatureSerializer(data=request.data)
        if serializer.is_valid():
            signature = serializer.save()
            if signature.is_valid:
                result = {"signature": "valid"}
            else:
                result = {"signature": "invalid"}
            return Response(result)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["POST"], url_path="approve")
    def approve_loan(self, request, pk=None):
        """
        POST to this endpoint with LoanRequestReview to approve the signed loan.
        This view will create a LoanRequestReview
        instance, and save it, triggering the Loan.state change as appropriate.
        This action need permission `zw_approve_loan`
        """
        # context is set so that serializer can access request.user
        serializer = LoanRequestReviewSerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            # saving LoanRequestReview will change state of loan
            serializer.save()
            # can't think of a case where pk is None
            state = Loan.objects.get(pk=pk).state
            return Response({"result": state}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RepaymentScheduleLineViewSet(viewsets.ModelViewSet):
    """
    API endpoint to view and edit Loan schedule lines. Not to be used
    directly, but as part of a loan creation/edit.
    """

    serializer_class = RepaymentScheduleLineSerializer
    permission_classes = (IsAgentOrStaff,)

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            return RepaymentScheduleLine.objects.all()
        else:
            return RepaymentScheduleLine.objects.filter(
                loan__borrower__agent__user=user
            )


class RepaymentViewSet(viewsets.ModelViewSet):
    """
    API endpoint to view and edit Loan repayments.
    """

    serializer_class = RepaymentSerializer
    permission_classes = (IsAgentOrStaff,)

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return None

        if user.is_staff:
            return Repayment.objects.all()
        else:
            return Repayment.objects.filter(loan__borrower__agent__user=user)

    @action(detail=True, methods=["get"], url_path="full")
    def get_full_repayment(self, request, pk=None):
        """
        Return the full detail of repayment.
        """
        if not request.user.is_authenticated:
            raise NotAuthenticated()

        obj = Repayment.objects.get(pk=pk)
        return Response(RepaymentFullSerializer(obj).data)


class ReasonForDelayedRepaymentViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    """
    API endpoint for reasons for delay in repayments.
    GET the list to display the available options.
    POST if a non-listed reason is added by the user.
    """

    serializer_class = ReasonForDelayedRepaymentSerializer
    permission_classes = (IsAgentOrStaff,)
    queryset = ReasonForDelayedRepayment.objects.all()


class DisbursementViewSet(viewsets.ModelViewSet):
    """
    API endpoint to submit a disbursement request and get details about
    the latest one an agent received.
    """

    serializer_class = DisbursementSerializer
    permission_classes = (IsAgentOrStaff,)

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return None

        if user.is_staff:
            return Disbursement.objects.all()
        else:
            # return only the latest object, we use slicing instead of .first()
            # to get a list instead of a single object (in the future, we'll want
            # the app to handle more than 1 item
            return Disbursement.objects.filter(
                disbursed_to__user=user, state=DISBURSEMENT_SENT
            ).order_by("-timestamp")[0:1]

    @action(detail=True,
        methods=["PATCH"],
        permission_classes=[DisbursePermissions],
        url_path="disburse")
    def disburse(self, request, pk):
        """
        API endpoint to change Disbursement state from
        DISBURSEMENT_REQUESTED to DISBURSEMENT_SENT
        Expected format:
        {
            "provider_transaction_id": <transaction_id>,    (this is not needed for DISB_METHOD_CASH_AT_AGENT)
            "transfer": <transfer_pk>
        }
        """
        disbursement = Disbursement.objects.get(pk=pk)
        # do not use DisbursementSerializer for this one since validations are different
        serializer = DisbursementDisburseSerializer(disbursement, data=request.data)
        if serializer.is_valid():
            # saving Disbursement object with state DISBURSEMENT_SENT will also change state of Loan
            serializer.save(state=DISBURSEMENT_SENT)
            return Response(serializer.data)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class NotificationView(views.APIView):
    """
    API endpoint to get a list of notifications for the requesting User.
    Read only.
    """

    serializer_class = NotificationSerializer
    permission_classes = (IsAgentOrStaff,)

    def get(self, request, format=None):
        if not request.user.is_authenticated:
            return None

        notifs = Notification.objects.filter(loan__borrower__agent__user=request.user)
        data = NotificationSerializer(notifs, many=True).data
        # delete notifications so we don't send them out twice
        # this is an OK side effect for a GET request as it remains
        # idempotent: no new data is generated by GETting again and again
        # FIXME: temporary: do not delete notifications while we're building the code
        # for n in notifs:
        #    n.delete()
        return Response(data)


class SuperUsertoLenderPaymentCustomView(views.APIView):
    """
    This endpoint will create multiple objects (not only SuperUsertoLenderPayment)
    Expected format:
    {
        amount: <money amount>
        method: <method of money transfer>
        details: <detail info for associated with method>
        repayments: [id(s) of repayments]
    }
    more info about 'method' and 'details' can be found in 'payments.models.Transfer'
    """

    permission_classes = (SuperUserOnlyView,)

    def post(self, request, format=None):
        # general structure of this code is copied from `WavePaysbuyViewset`
        ser = SuperUsertoLenderPaymentCustomSerializer(data=request.data)
        if ser.is_valid():
            try:
                # for now, only allow method 5 (Pay with Wave)
                if ser.validated_data["method"] != TRANSFER_METHOD_PAY_WITH_WAVE:
                    return Response(
                        {"success": False, "error": "transfer method not implemented"},
                        status=status.HTTP_501_NOT_IMPLEMENTED,
                    )

                # make sure details have 'sender_phone_number'
                if "sender_phone_number" not in ser.validated_data["details"]:
                    return Response(
                        {"success": False, "error": "sender_phone_number required"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                repayments = Repayment.objects.filter(
                    id__in=ser.validated_data["repayments"]
                )
                # check if money amount is equal to sum of repayments
                if ser.validated_data["amount"] != sum([r.amount for r in repayments]):
                    return Response(
                        {
                            "success": False,
                            "error": "money amount is not equal to sum of repayments",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # check double payment (or) check if some repayments are already repaid
                for repayment in repayments:
                    su2lender = repayment.superuser_to_lender_payment
                    if su2lender:
                        try:
                            # Avoid dual repayment if already success
                            payment = su2lender.transfer
                            if payment.transfer_successful:
                                logger = logging.getLogger("root")
                                logger.error(
                                    "Double payment",
                                    extra={
                                        "repayment_id": repayment.id,
                                        "payment_detail": payment.details,
                                    },
                                )
                                return Response(
                                    {
                                        "error": "Repayment already successful",
                                        "success": False,
                                        "repayment_id": repayment.id,
                                    }
                                )

                        # If Transfer object not found in su2lender_payments
                        except Exception as e:
                            logger = logging.getLogger("root")
                            logger.error(
                                "Payments not found in su2lenderPayment object",
                                extra={"su2lender_id": su2lender.id, "Exception": e},
                            )

                # create transfer
                transfer = Transfer.objects.create(
                    amount=ser.validated_data["amount"],
                    user=request.user,
                    method=ser.validated_data["method"],
                    details=ser.validated_data["details"],
                )

                # trigger api call to Wave
                transfer.pay_with_wave()
                transfer.save()

                # create SuperUsertoLenderPayment
                super_user = request.user.agent
                lender = super_user.field_officer.mfi_branch
                loans = [r.loan for r in repayments]
                su_to_lender_payment = SuperUsertoLenderPayment(
                    super_user=super_user, lender=lender, transfer=transfer
                )
                su_to_lender_payment.save()
                su_to_lender_payment.loans.add(*loans)

                # FK to this SuperUsertoLenderPayment from repayments
                # this is just temporary link. FKs should be removed if transfer is failed.
                # this is done in PayWithWaveCallback api_view
                if transfer.transfer_successful is None:
                    for r in repayments:
                        r.superuser_to_lender_payment = su_to_lender_payment
                        r.save(no_checks=True)
            except Exception as e:
                logger = logging.getLogger("root")
                logger.error("unknown error", extra={"error": e})
                return Response(
                    {"success": False, "error": "Unknown Error"},
                    status=status.HTTP_200_OK,
                )
        else:
            result = ser.errors
            result["success"] = False
            logger = logging.getLogger("root")
            logger.error("serializer error", extra={"errors": ser.errors})
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        if transfer.transfer_successful is None:
            return Response(
                {
                    "success": True,
                    "su2lender_id": su_to_lender_payment.id,
                    "amount": ser.validated_data["amount"],
                },
                status=status.HTTP_201_CREATED,
            )
        else:
            # if we return a 400 code here it seems to cancel the database operation
            # ie: the save transaction seems to be rolled back because of this
            # see https://stackoverflow.com/questions/46704972/django-http-status-400-response-rolls-back-model-save
            return Response(
                {"success": False, "error": "transaction fail"},
                status=status.HTTP_200_OK,
            )


class ReconciliationView(views.APIView):
    """
    API endpoints for reconciliation
    """

    permission_classes = (IsAdminUser,)

    def get(self, request, *args, **kwargs):
        """
        parameters
        superuser: <superuser id>
        start-date: <start date - iso8601 format:YYYY-MM-DD e.g. 2012-09-27>
        days: <int - number of days>

        example
        api/v1/reconciliation-api/?superuser=1&start-date=2016-10-30&days=15

        all those parameters are optional and default values are
        superuser: all superuser
        start_date: today
        days: 30

        return
        [
            {
                superuser: <superuser name>
                repayments: <Repayment objects>
                superusertolenderpayments: <SuperUsertoLenderPayment objects>
            },
            {
                superuser: <superuser name>
                repayments: <Repayment objects>
                superusertolenderpayments: <SuperUsertoLenderPayment objects>
            },
            {
                superuser: <superuser name>
                repayments: <Repayment objects>
                superusertolenderpayments: <SuperUsertoLenderPayment objects>
            },
            .........
        ]
        """
        try:
            # process parameters
            superusers = Agent.objects.all()
            if "superuser" in request.query_params:
                # use filter so that it return iterable(queryset) and consistent in looping later
                superusers = Agent.objects.filter(
                    id=int(request.query_params["superuser"])
                )
            start_date = d.today()
            if "start-date" in request.query_params:
                start_date = request.query_params["start-date"]
                start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            days = 30
            if "days" in request.query_params:
                days = int(request.query_params["days"])
            end_date = start_date + timedelta(days=days)

            # filter data
            result = []
            for su in superusers:
                repayments = (
                    Repayment.objects.filter(date__gte=start_date, date__lte=end_date)
                    .filter(
                        loan__borrower__agent=su,
                        reconciliation_status__in=[
                            NOT_RECONCILED,
                            NEED_MANUAL_RECONCILIATION,
                        ],
                    )
                    .order_by("-date")
                )
                su2lpayments = (
                    SuperUsertoLenderPayment.objects.filter(
                        transfer__timestamp__date__gte=start_date,
                        transfer__timestamp__date__lte=end_date,
                    )
                    .filter(
                        super_user=su,
                        reconciliation_status__in=[
                            NOT_RECONCILED,
                            NEED_MANUAL_RECONCILIATION,
                        ],
                    )
                    .order_by("-transfer__timestamp")
                )
                repayments = RepaymentSerializer(repayments, many=True)
                su2lpayments = SuperUsertoLenderPaymentSerializer(
                    su2lpayments, many=True
                )
                result.append(
                    {
                        "superuser": su.name,
                        "superuser_id": su.id,
                        "repayments": repayments.data,
                        "superusertolenderpayments": su2lpayments.data,
                    }
                )

            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request):
        """
        This endpoint is for manual reconciliation
        Expected format:
        {
            repayments: [id(s) of Repayments]
            su2lpayments: [id(s) of SuperUsertoLenderPayments]
        }
        return reconciliation id if success

        validation
        it will check
        if sum of repayments == sum of su2lpayments and
        if all data are from same SuperUser.
        if not, error will be raised.
        """
        ser = ReconciliationPOSTSerializer(data=request.data)
        if ser.is_valid():
            repayments = Repayment.objects.filter(
                id__in=ser.validated_data["repayments"]
            )
            su2lpayments = SuperUsertoLenderPayment.objects.filter(
                id__in=ser.validated_data["su2lpayments"]
            )
            recon_obj = reconcile_with_intermediary(
                repayments,
                su2lpayments,
                reconciled_by=request.user,
                method=MANUAL_RECONCILED,
            )
            return Response(
                {"success": True, "recon_id": recon_obj.id},
                status=status.HTTP_201_CREATED,
            )
        else:
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)


class ReconciliationV2View(views.APIView):
    """
    API endpoints for reconciliation
    """

    permission_classes = (IsAdminUser,)

    def get(self, request, *args, **kwargs):
        """
        parameters
        superuser: <superuser id>
        start-date: <start date - iso8601 format:YYYY-MM-DD e.g. 2012-09-27>
        days: <int - number of days>

        example
        api/v1/reconciliation-api/?superuser=1&start-date=2016-10-30&days=15

        all those parameters are optional and default values are
        superuser: all superuser
        start_date: today
        days: 30

        return
        [
            {
                superuser: <superuser name>
                repayments: <Repayment objects>
                superusertolenderpayments: <SuperUsertoLenderPayment objects>
            },
            {
                superuser: <superuser name>
                repayments: <Repayment objects>
                superusertolenderpayments: <SuperUsertoLenderPayment objects>
            },
            {
                superuser: <superuser name>
                repayments: <Repayment objects>
                superusertolenderpayments: <SuperUsertoLenderPayment objects>
            },
            .........
        ]
        """
        try:
            # process parameters
            superusers = Agent.objects.all()
            if "superuser" in request.query_params:
                # use filter so that it return iterable(queryset) and consistent in looping later
                superusers = Agent.objects.filter(
                    id=int(request.query_params["superuser"])
                )
            start_date = d.today()
            if "start-date" in request.query_params:
                start_date = request.query_params["start-date"]
                start_date = datetime.strptime(start_date, "%Y-%m-%d").date()

            days = 30
            end_date = start_date + timedelta(days=days)

            if "end-date" in request.query_params:
                end_date = request.query_params["end-date"]
                end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

            query_obj = [
                "not reconciled",
                "auto reconciled",
                "manual reconciled",
                "need manual reconciliation",
            ]
            if "show-linked" in request.query_params:
                linked = request.query_params["show-linked"]
                if linked == "TRUE":
                    query_obj = ["auto reconciled", "manual reconciled"]
                if linked == "FALSE":
                    query_obj = ["not reconciled", "need manual reconciliation"]

            # filter data
            result = []
            for su in superusers:
                repayments = (
                    Repayment.objects.filter(date__gte=start_date, date__lte=end_date)
                    .filter(
                        loan__borrower__agent=su, reconciliation_status__in=query_obj
                    )
                    .order_by("-date")
                )
                su2lpayments = (
                    SuperUsertoLenderPayment.objects.filter(
                        transfer__timestamp__date__gte=start_date,
                        transfer__timestamp__date__lte=end_date,
                    )
                    .filter(super_user=su, reconciliation_status__in=query_obj)
                    .order_by("-transfer__timestamp")
                )
                repayments = NewRepaymentSerializer(repayments, many=True)
                su2lpayments = SuperUsertoLenderPaymentSerializer(
                    su2lpayments, many=True
                )
                result.append(
                    {
                        "superuser": su.name,
                        "superuser_note": su.note,
                        "superuser_id": su.id,
                        "repayments": repayments.data,
                        "superusertolenderpayments": su2lpayments.data,
                    }
                )

            payments = (
                SuperUsertoLenderPayment.objects.filter(
                    transfer__timestamp__date__gte=start_date,
                    transfer__timestamp__date__lte=end_date,
                )
                .filter(transfer__user__username="Anonymous_User")
                .order_by("-transfer__timestamp")
            )

            transfers = SuperUsertoLenderFullPaymentSerializer(payments, many=True)
            transfer = []
            transfer.append({"transfers": transfers.data})
            data = {"transfer": transfer, "result": result}
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class CreateNoteView(views.APIView):
    """
    API endpoints to create Note for objects
    input:
        {
            note: 'Im writing note'
            borrower: '212',
              OR
            agent: 32,
              OR
            line:32,
              OR
            repyament: 23,
              OR
            transfer : 23
        }
    """

    permission_classes = (IsAdminUser,)

    def post(self, request):
        serializer = NoteSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            try:
                data = serializer.validated_data
                note = data.get("note")
                borrower = data.get("borrower")
                if borrower:
                    Borrower.objects.filter(id=borrower).update(comments=note)

                agent = data.get("agent")
                if agent:
                    Agent.objects.filter(id=agent).update(note=note)

                lines = data.get("lines")
                if lines:
                    RepaymentScheduleLine.objects.filter(id=lines).update(note=note)

                transfer = data.get("transfer")
                if transfer:
                    Transfer.objects.filter(id=transfer).update(note=note)

                repayments = data.get("repayments")
                if repayments:
                    Repayment.objects.filter(id=repayments).update(note=note)
                return Response(
                    {"message": "Update successful"}, status=status.HTTP_202_ACCEPTED
                )
            except Exception as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BorrowerLoanView(views.APIView):
    """
    This api provide all borrower loans
    """

    permission_classes = (IsAdminUser,)

    def create_data(self, loan, lines=None, repayments=None):
        result = []
        if lines and repayments:
            lines = RepaymentScheduleLineSerializer(lines, many=True)
            repayments = NewRepaymentSerializer(repayments, many=True)
            result.append(
                {
                    "loan_id": loan.id,
                    "contract_date": loan.contract_date,
                    "borrower_id": loan.borrower.id,
                    "borrower_name": loan.borrower.name_en,
                    "repayments": repayments.data,
                    "schedule_lines": lines.data,
                }
            )
        return result

    def get(self, request, *args, **kwargs):
        try:
            result = []
            if "borrower" in request.query_params:
                # use filter so that it return iterable(queryset) and consistent in looping later
                loans = Loan.objects.filter(
                    borrower__id=int(request.query_params["borrower"])
                )
                if "ALL" in request.query_params:
                    for loan in loans:
                        lines = loan.lines.all()
                        repayments = loan.repayments.all()
                        result = self.create_data(loan, lines, repayments)

                elif "Date-Range" in request.query_params:
                    if "start-date" in request.query_params:
                        start_date = request.query_params["start-date"]
                        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()

                    if "end-date" in request.query_params:
                        end_date = request.query_params["end-date"]
                        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

                    for loan in loans:
                        lines = loan.lines.filter(
                            date__lte=end_date, date__gte=start_date
                        )
                        repayments = loan.repayments.filter(
                            date__lte=end_date, date__gte=start_date
                        )

                        result = self.create_data(loan, lines, repayments)

                elif "Unallocated" in request.query_params:
                    loans = loans.all().order_by("-id")[:1]
                    for loan in loans:
                        lines = loan.lines.all()
                        repayments = loan.repayments.all()
                        result = self.create_data(loan, lines, repayments)

            data = {"loans": result}
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class CashTransferView(views.APIView):

    serializer_class = CashTransferSerializer
    permission_classes = (IsAgentOrStaff,)

    def post(self, request, *args, **kwargs):
        """
        """
        serializer = CashTransferSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            try:
                data = serializer.validated_data
                agent = data.get("agent")
                agent_obj = Agent.objects.get(id=agent)
                data.pop("agent")

                loans = Loan.objects.all()[0]
                transfer = Transfer.objects.create(
                    transfer_successful=True,
                    method=TRANSFER_METHOD_CASH_IN_HAND,
                    user=agent_obj.user,
                    details={},
                    **data
                )
                lender = agent_obj.field_officer.mfi_branch
                su_2_le = SuperUsertoLenderPayment(
                    super_user=agent_obj, transfer=transfer, lender=lender
                )
                su_2_le.save()
                su_2_le.loans.add(*loans)

                agent.last_money_transfer = datetime.now(timezone.utc)
                agent.save()
                return Response({"success": True}, status=status.HTTP_201_CREATED)
            except Exception:
                return Response({"success": False}, status=status.HTTP_200_OK)

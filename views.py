import datetime
import logging
from datetime import date as d
from datetime import timedelta
from decimal import *

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.forms import ModelForm
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import translation
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework.request import Request
from wkhtmltopdf.views import PDFTemplateView

from borrowers.api_views import TodayView
from borrowers.models import Agent, Borrower
from loans.models import Disbursement, Loan, PhotoSignature
from org.models import MFI
from payments.models import (TRANSFER_METHOD_PAY_WITH_WAVE,
                             TRANSFER_METHOD_WAVE_TO_WAVE, Transfer)
from sms_gateway.models import WaveMoneyReceiveSMS

from .models import (LOAN_DISBURSED, LOAN_REPAID, LOAN_REQUEST_APPROVED,
                     LOAN_REQUEST_DRAFT, LOAN_REQUEST_REJECTED,
                     LOAN_REQUEST_SIGNED, LOAN_REQUEST_SUBMITTED, Loan,
                     Repayment, RepaymentScheduleLine,
                     SuperUsertoLenderPayment)


@login_required
def clean_collection_report_arguments(request):
    """
    Build a collection sheet for an agent, to be printed. This is useless for electronic collection :)
    NOTE: this works only with a single loan per borrower, it is most likely broken if a borrower has
    more than one loan over the date range.
    """
    if request.method == "GET":
        # get the first day from url GET parameter (eg: url/?date=2016-10-30)
        try:
            day = [int(x) for x in request.GET.get("date").split("-")]
            first_day = d(day[0], day[1], day[2])
        except:
            first_day = d.today()

        agent_pk = request.GET.get("agent")

        try:
            num_days = int(request.GET.get("days"))
        except:
            num_days = 7

        try:
            show_paid = request.GET.get("show_paid")
            show_paid = not (show_paid in ["false", "no"])
        except:
            show_paid = True
        return first_day, agent_pk, num_days, show_paid


def get_collection_sheet_context(first_day, agent_pk, num_days, show_paid):
    """
    this function expect sanitized arguments, or it may break badly :(
    """
    try:
        agent = Agent.objects.get(pk=agent_pk)
    except Agent.DoesNotExist:
        agent = None
    last_day = first_day + timedelta(days=num_days)
    previous_week_start = first_day + timedelta(days=-7)

    # get all repayments scheduled between the two dates
    # but remove planned repayment after the loan is repaid.
    line_queryset = (
        RepaymentScheduleLine.objects.filter(date__gte=first_day, date__lte=last_day)
        .filter(loan__state__in=["disbursed", "repaid"])
        .filter(
            Q(loan__repaid_on__isnull=True) | Q(loan__repaid_on__isnull=False) & Q(date__lte=F("loan__repaid_on"))
        )
        .order_by("date")
        .order_by("loan__borrower__name_en")
    )

    # get the list of repayments received in the period too
    # so we can show unexpected repayments
    repayments_received = (
        Repayment.objects.filter(date__gte=first_day, date__lte=last_day)
        .order_by("date")
        .order_by("loan__borrower__name_en")
    )

    # narrow down the results to the agent requested
    if agent_pk is not None:
        line_queryset = line_queryset.filter(loan__borrower__agent__pk=agent_pk)
        repayments_received = repayments_received.filter(
            loan__borrower__agent__pk=agent.pk
        )

    # TODO: add missed payments

    borrower_set = set()
    for line in line_queryset:
        borrower_set.add(line.loan.borrower)
    for r in repayments_received:
        borrower_set.add(r.loan.borrower)
    borrower_list = sorted(list(borrower_set), key=lambda x: x.name_en.lower())

    date_range = [first_day + timedelta(days=x) for x in range(num_days)]

    collection_list = []
    # [[0,0]] * num_days is avoided because of this
    # https://stackoverflow.com/questions/240178/list-of-lists-changes-reflected-across-sublists-unexpectedly
    # 0th index of internal list is for planned repayment
    # 1st index of internal list is for repaid repayment
    daily_totals = [[0, 0, 0, 0] for _ in range(num_days)]

    for borrower in borrower_list:
        repayments = []
        for idx, day in enumerate(date_range):
            # repayment = sum(line.principal + line.fee + line.interest + line.penalty for line in [l for l in line_queryset if l.loan.borrower == borrower and l.date == day])
            repaid = (
                Repayment.objects.filter(loan__borrower=borrower, date=day).aggregate(
                    Sum("amount")
                )["amount__sum"] or 0
            )

            repayment = subscription = principal = fee = interest = penalty = 0
            for line in line_queryset:
                if line.loan.borrower == borrower and line.date == day:
                    repayment += (
                        line.principal + line.fee + line.interest + line.penalty
                    )
                    subscription += line.subscription
                    principal += line.principal
                    fee += line.fee
                    interest += line.interest
                    penalty += line.penalty

            try:
                transferd = sum(
                    [
                        repayment.amount
                        for repayment in Repayment.objects.filter(
                            loan__borrower=borrower, date=day
                        )
                        if repayment.reconciliation_id
                    ]
                )
                # For old data when reconciliation process was not there
                if transferd == 0:
                    # It's working only if payment using pay-with-wave money for collection sheet
                    transferd = sum(
                        [
                            repayment.amount
                            for repayment in Repayment.objects.filter(
                                loan__borrower=borrower, date=day
                            )
                            if repayment.superuser_to_lender_payment.transfer.transfer_successful
                        ]
                    )
            except Exception as e:
                transferd = 0

            repayments.append(
                (
                    repayment,
                    repaid,
                    transferd,
                    subscription,
                    principal,
                    fee,
                    interest,
                    penalty,
                )
            )

            daily_totals[idx][0] += repayment
            daily_totals[idx][1] += repaid
            daily_totals[idx][2] += transferd
            daily_totals[idx][3] += subscription

        try:
            contract_number = borrower.get_current_loan_at(first_day).contract_number
        except AttributeError:
            contract_number = None
        row = {
            "borrower": borrower,
            "contract_number": contract_number,
            "repayments": repayments,
        }
        collection_list.append(row)

    # total money sent by agent via wave money
    wave_totals = [0] * num_days
    for idx, day in enumerate(date_range):
        if agent is not None:
            wave = (
                WaveMoneyReceiveSMS.objects.filter(
                    sender=agent.wave_money_number, sent_at__date=day
                ).aggregate(Sum("amount"))["amount__sum"] or 0
            )
        else:
            wave = (
                WaveMoneyReceiveSMS.objects.filter(sent_at__date=day).aggregate(
                    Sum("amount")
                )["amount__sum"] or 0
            )
        wave_totals[idx] = wave

    # build a list of dates in burmese as the translation doesn't seem to work
    # (pulling the forked django isn't working from gitlab)
    burmese_dates = []
    burmese_months = [
        "ဇန္နဝါရီလ",
        "ဖေဖေါ်ဝါရီလ ",
        "မတ်လ",
        "april",
        "may",
        "ဇွန်လ",
        "ဇူလိုင်လ",
        "သြဂုတ်လ",
        "စက်တင်ဘာလ",
        "အောက်တိုဘာလ",
        "နိုဝင်ဘာလ",
        "ဒီဇင်ဘာလ",
    ]
    for day in date_range:
        bd = "{0} {1} {2}".format(day.day, burmese_months[day.month - 1], day.year)
        burmese_dates.append(bd)

    # superuser to lender transactions
    su_wave_numbers = Agent.objects.exclude(wave_money_number="").values_list(
        "wave_money_number", flat=True
    )
    transactions = []
    total_transactions = 0
    for day in date_range:
        # select known transactions
        via_wave_to_wave = WaveMoneyReceiveSMS.objects.filter(
            sent_at__date=day, sender__in=su_wave_numbers
        ).order_by("sent_at")
        via_pay_with_wave = (
            SuperUsertoLenderPayment.objects.filter(
                transfer__timestamp__date=day, transfer__transfer_successful=True
            )
            .exclude(transfer__method=TRANSFER_METHOD_WAVE_TO_WAVE)
            .order_by("transfer__timestamp")
        )
        if agent_pk is not None:
            via_wave_to_wave = via_wave_to_wave.filter(sender=agent.wave_money_number)
            via_pay_with_wave = via_pay_with_wave.filter(super_user=agent)
        total_wave_to_wave = (
            via_wave_to_wave.aggregate(Sum("amount"))["amount__sum"] or 0
        )
        total_pay_with_wave = (
            via_pay_with_wave.aggregate(Sum("transfer__amount"))[
                "transfer__amount__sum"
            ] or 0
        )
        total = total_wave_to_wave + total_pay_with_wave
        total_transactions += total
        data_per_day = {
            "date": day,
            "via_wave_to_wave": via_wave_to_wave,
            "via_pay_with_wave": via_pay_with_wave,
            "total_wave_to_wave": total_wave_to_wave,
            "total_pay_with_wave": total_pay_with_wave,
            "total": total,
        }
        transactions.append(data_per_day)

    # unknown transactions (this is possible when transfer via wave agent)
    # TODO: make superuser field of su2lender_payment accept empty value
    unknown_transactions = []
    total_unknown_transactions = 0
    for day in date_range:
        # select known transactions
        via_wave_to_wave = (
            WaveMoneyReceiveSMS.objects.filter(sent_at__date=day)
            .exclude(sender__in=su_wave_numbers)
            .order_by("sent_at")
        )
        total_wave_to_wave = (
            via_wave_to_wave.aggregate(Sum("amount"))["amount__sum"] or 0
        )
        total = total_wave_to_wave
        total_unknown_transactions += total
        data_per_day = {
            "date": day,
            "via_wave_to_wave": via_wave_to_wave,
            "total_wave_to_wave": total_wave_to_wave,
            "total": total,
        }
        unknown_transactions.append(data_per_day)

    context = {
        "collection_list": collection_list,
        "daily_totals": daily_totals,
        "wave_totals": wave_totals,
        "date_range": date_range,
        "first_day": first_day,
        "last_day": last_day,
        "previous_week_start": previous_week_start,
        "burmese_dates": burmese_dates,
        "show_paid": show_paid,
        "agent": agent,
        "transactions": transactions,
        "total_transactions": total_transactions,
        "unknown_transactions": unknown_transactions,
        "total_unknown_transactions": total_unknown_transactions,
    }
    return context


class ReconciliationHighLevelView(LoginRequiredMixin, View):
    """
    Display the High Level report for reconciliation
    """

    def get(self, request):
        translation.activate("my")
        output = render(
            request,
            "loans/reconciliation-high-level.html",
            get_collection_sheet_context(*clean_collection_report_arguments(request)),
        )
        translation.deactivate()
        return output


class CollectionSheetView(LoginRequiredMixin, View):
    """
    Display the collection sheet
    """

    def get(self, request):
        # activate burmese l10n just for this report
        translation.activate("my")
        output = render(
            request,
            "loans/collection_report.html",
            get_collection_sheet_context(*clean_collection_report_arguments(request)),
        )
        translation.deactivate()
        return output


class CollectionSheetPDFView(LoginRequiredMixin, PDFTemplateView):
    filename = "collection_sheet.pdf"
    template_name = "loans/collection_report.html"
    cmd_options = {
        "javascript-delay": 500,
        "orientation": "landscape",
    }

    def get(self, request, *args, **kwargs):
        first_day, agent_pk, num_days, show_paid = clean_collection_report_arguments(
            request
        )
        self.filename = (
            "collection_sheet_" + str(agent_pk) + "_" + str(first_day) + ".pdf"
        )
        context = self.get_context_data(**kwargs)
        # merge the 2 dictionaries in a single one. requires python 3.5
        # see https://treyhunner.com/2016/02/how-to-merge-dictionaries-in-python/
        context = {
            **context,
            **get_collection_sheet_context(first_day, agent_pk, num_days, show_paid),
        }
        return self.render_to_response(context)


# TODO: add csrf handling here, this is not super safe
@csrf_exempt
def register_payment(request):
    """
    Super simple form used to record a repayment when clicking the "pay" link in the
    repayment sheet (request comes as AJAX)
    """
    if request.method == "POST":
        try:
            r = Repayment()
            r.amount = Decimal(request.POST.get("amount"))
            r.loan = Loan.objects.get(pk=request.POST.get("loan"))
            day = [int(x) for x in request.POST.get("date").split("-")]
            r.date = d(day[0], day[1], day[2])
            r.recorded_by = request.user
            r.save()
        except Exception as e:
            logger = logging.getLogger("root")
            logger.error(
                "register_payment error",
                exc_info=True,
                extra={"request": request, "exception": e},
            )
            return JsonResponse(
                {"error": "{0}: {1!r}".format(type(e).__name__, e.args)}
            )
        else:
            return JsonResponse({"result": "ok", "repayment_id": r.pk})
    if request.method == "GET":
        return JsonResponse({"error": "invalid request"})


@login_required
def repayment_sheet(request):
    """
    the same as the collection sheet, but empty cells are replaced with a gimmick to input repayments
    and we only show one day at a time
    """
    if request.method == "GET":
        # get the day from url GET parameter (eg: url/?date=2016-10-30)
        try:
            day = [int(x) for x in request.GET.get("date").split("-")]
            day = d(day[0], day[1], day[2])
        except:
            day = d.today()

        try:
            agent_pk = request.GET.get("agent")
            if agent_pk == "":  # happens if url contains ?agent=&date=...
                agent_pk = None
        except:
            # no agent specified
            agent_pk = None

        num_days = 1

        # get all repayments scheduled on that day
        # FIXME: this should be the outstanding amount (principal+fee)!!
        line_queryset = (
            RepaymentScheduleLine.objects.filter(date__gte=day)
            .order_by("date")
            .order_by("loan__borrower__name_en")
            .filter(
                # ignore loans that have already been repaid. If we input the last repayment for X, it closes
                # the loan. Reloading the page at that point causes an error further down when trying to find
                # X's current loan as it is now closed
                loan__repaid_on=None
            )
        )
        # a specific agent was requested, filter for it
        if agent_pk is not None:
            line_queryset = line_queryset.filter(loan__borrower__agent__pk=agent_pk)
        # TODO: add missed payments

        borrower_set = set()
        for line in line_queryset:
            borrower_set.add(line.loan.borrower)
        borrower_list = sorted(list(borrower_set), key=lambda x: x.name_en.lower())

        collection_list = []
        for borrower in borrower_list:
            scheduled_repayment = sum(
                line.principal + line.fee
                for line in [
                    l
                    for l in line_queryset
                    if l.loan.borrower == borrower and l.date == day
                ]
            )

            actual_repayment = sum(
                r.amount
                for r in Repayment.objects.filter(loan__borrower=borrower).filter(
                    date=day
                )
            )

            current_loan = borrower.get_current_loan_at(day)
            try:
                contract_number = current_loan.contract_number
                loan_pk = current_loan.pk
            except AttributeError:
                contract_number = None
                loan_pk = None
            row = {
                "borrower": borrower,
                "contract_number": contract_number,
                "loan_pk": loan_pk,
                "scheduled_repayment": scheduled_repayment,
                "actual_repayment": actual_repayment,
            }
            collection_list.append(row)

        context = {
            "collection_list": collection_list,
            "day": day,
        }
        return render(request, "loans/repayment_sheet.html", context)


class DisbursementForm(ModelForm):
    class Meta:
        model = Disbursement
        fields = ["amount", "disbursed_to", "fees_paid", "method"]
        labels = {
            "fees_paid": _("Fees paid (to Wave, Bank, Agent, etc...)"),
        }


@login_required
def request_sheet(request):
    if request.method == "GET":
        # checking which mfi to use
        # check if user is staff, use ZigWayMFI.
        # Otherwise, use the mfi of user.
        # If user don't hv mfi, return empty report
        if request.user.is_staff:
            mfi = MFI.objects.get(name="ZigWayMFI")
        else:
            try:
                mfi = request.user.agent.field_officer.mfi_branch.mfi
            except ObjectDoesNotExist:
                context = {
                    "collection_list": [],
                    "day": d.today(),
                }
                return render(request, "loans/request_sheet.html", context)

        # filtering loans according to state and mfi
        loan_request_queryset = Loan.objects.filter(
            Q(state=LOAN_REQUEST_DRAFT) |
            Q(state=LOAN_REQUEST_SUBMITTED) |
            Q(state=LOAN_REQUEST_SIGNED) |
            Q(state=LOAN_REQUEST_REJECTED),
            borrower__agent__field_officer__mfi_branch__mfi=mfi,
        )

        # by ordering queryset, submitted loans will be at top. Then sort by name
        loan_request_queryset = loan_request_queryset.order_by(
            "borrower__agent__name", "-state"
        )

        # extracting necessary data from each loan
        collection_list = []
        for loan_request in loan_request_queryset:

            signatures = PhotoSignature.objects.filter(loan=loan_request)
            if signatures.exists():
                signature = signatures.latest("timestamp")
            else:
                signature = None

            row = {
                "agent": loan_request.borrower.agent,
                "borrower": loan_request.borrower,
                "loan": loan_request,
                "signature": signature,
            }
            collection_list.append(row)

        context = {
            "collection_list": collection_list,
            "day": d.today(),
        }
        return render(request, "loans/request_sheet.html", context)


@login_required
def disburse_sheet(request):
    if request.method == "GET":
        # checking which mfi to use
        # check if user is staff, use ZigWayMFI.
        # Otherwise, use the mfi of user.
        # If user don't hv mfi, return empty report
        if request.user.is_staff:
            mfi = MFI.objects.get(name="ZigWayMFI")
        else:
            try:
                mfi = request.user.agent.field_officer.mfi_branch.mfi
            except ObjectDoesNotExist:
                context = {
                    "collection_dict": [],
                    "day": d.today(),
                }
                return render(request, "loans/disburse_sheet.html", context)

        # filtering loans according to state and mfi
        loan_request_queryset = Loan.objects.filter(
            Q(state=LOAN_REQUEST_APPROVED),
            borrower__agent__field_officer__mfi_branch__mfi=mfi,
        )

        # by ordering queryset, submitted loans will be at top. Then sort by name
        loan_request_queryset = loan_request_queryset.order_by(
            "borrower__agent__name", "-state"
        )

        # extracting necessary data from each loan
        collection_dict = {}
        for loan_request in loan_request_queryset:
            try:
                signature = PhotoSignature.objects.get(loan=loan_request)
            except PhotoSignature.DoesNotExist:
                signature = None

            row = {
                "agent": loan_request.borrower.agent,
                "borrower": loan_request.borrower,
                "loan": loan_request,
                "signature": signature,
            }

            # group by agent
            agent = loan_request.borrower.agent.name
            if agent not in collection_dict:
                collection_dict[agent] = [row]
            else:
                collection_dict[agent].append(row)

        context = {
            "collection_dict": collection_dict,
            "day": d.today(),
            "form": DisbursementForm(),
        }
        return render(request, "loans/disburse_sheet.html", context)


@login_required
def outstanding_loans(request):
    if request.method == "GET":
        qs = (
            Loan.objects.filter(state="disbursed")
            .annotate(actual_principal=Coalesce(Sum("repayments__principal"), 0))
            .annotate(actual_fee=Coalesce(Sum("repayments__fee"), 0))
            .annotate(outstanding=F("loan_amount") + F("loan_fee") - F("actual_principal") - F("actual_fee"))
            .order_by(F("outstanding").desc())
            .select_related("borrower")
        )
        # select_related is to make rendering faster
        total_outstanding = qs.aggregate(tot=Sum("outstanding"))["tot"]

        loans = [
            {"obj": l, "amount_due": l.total_amount_due_for_date(d.today())}
            for l in qs
        ]
        context = {
            "outstanding_loans": loans,
            "total_outstanding": total_outstanding,
            "current_date": d.today(),
        }
        return render(request, "loans/outstanding_loans.html", context)


@login_required
def late_loans(request):
    if request.method == "GET":
        from datetime import datetime
        start_date = request.GET.get("start_date")
        end_date = request.GET.get("end_date")
        if start_date and end_date:
            s_date = datetime.strptime(start_date, '%m/%d/%Y').strftime('%Y-%m-%d')
            e_date = datetime.strptime(end_date, '%m/%d/%Y').strftime('%Y-%m-%d')
            loans = Loan.objects.filter(repaid_on=None, contract_date__gte=s_date, contract_date__lte=e_date).filter(state="disbursed")
        elif start_date and (end_date is None):
            s_date = datetime.strptime(start_date, '%m/%d/%Y').strftime('%Y-%m-%d')
            loans = Loan.objects.filter(repaid_on=None, contract_date__gte=s_date).filter(state="disbursed")
        else:
            loans = Loan.objects.filter(repaid_on=None).filter(state="disbursed")
        late_loans = [
            {"obj": l, "total_days_late": l.current_delay_at(d.today())} for l in loans
        ]
        late_loans = [l for l in late_loans if l["total_days_late"] > 0]
        late_loans.sort(key=lambda x: x["total_days_late"])

        total_outstanding = 0
        for l in late_loans:
            if l["total_days_late"] <= 30:
                l["par_category"] = "PAR 1-30"
            elif l["total_days_late"] <= 60:
                l["par_category"] = "PAR 31-60"
            elif l["total_days_late"] <= 90:
                l["par_category"] = "PAR 61-90"
            else:
                l["par_category"] = "PAR >90"

            total_outstanding += l["obj"].total_outstanding

        context = {
            "late_loans": late_loans,
            "current_date": d.today(),
            "total_outstanding": total_outstanding,
        }
        return render(request, "loans/late_loans.html", context)


@login_required
def signed_loan_requests_for_disbursement_sheet(request):
    if request.method == "GET":
        # checking which mfi to use
        # check if user is staff, use ZigWayMFI.
        # Otherwise, use the mfi of user.
        # If user don't hv mfi, return empty report
        if request.user.is_staff:
            mfi = MFI.objects.get(name="ZigWayMFI")
        else:
            try:
                mfi = request.user.agent.field_officer.mfi_branch.mfi
            except ObjectDoesNotExist:
                context = {
                    "collection_list": [],
                    "day": d.today(),
                }
                return render(
                    request,
                    "loans/signed_loan_requests_for_disbursement_sheet.html",
                    context,
                )

        # filtering loans according to state and mfi. Then, order by agent name
        signed_loan_queryset = Loan.objects.filter(
            state=LOAN_REQUEST_APPROVED,
            borrower__agent__field_officer__mfi_branch__mfi=mfi,
        ).order_by("borrower__agent__name")

        # grouping loan by agent sum the total amount
        temp_list = signed_loan_queryset.values("borrower__agent__name").annotate(
            total_amount=Sum("loan_amount")
        )
        # processing the above list because the format is not convenient for extracting values inside template
        total_amount_by_agent = {}
        for dict in temp_list:
            total_amount_by_agent[dict["borrower__agent__name"]] = dict["total_amount"]

        # extracting necessary data from each loan
        collection_list = []
        for loan_request in signed_loan_queryset:
            row = {
                "agent": loan_request.borrower.agent,
                "borrower": loan_request.borrower,
                "loan": loan_request,
            }
            collection_list.append(row)

        context = {
            "collection_list": collection_list,
            "total_amount_by_agent": total_amount_by_agent,
            "day": d.today(),
        }
        return render(
            request, "loans/signed_loan_requests_for_disbursement_sheet.html", context
        )


@login_required
def collection_report2(request):
    """
    A collection report similar to BRAC's format, limited to a single day.
    """
    if request.method == "GET":
        try:
            day = [int(x) for x in request.GET.get("date").split("-")]
            date = d(day[0], day[1], day[2])
        except AttributeError:
            date = d.today()
        agent = None  # just return everything for now

        outstanding_loans = Loan.get_outstanding_loans_for_date(date)

        for loan in outstanding_loans:
            loan.outstanding_amount = loan.get_outstanding_amount_for_date(date)

        context = {
            "collection_list": outstanding_loans,
            "date": date,
        }
        return render(request, "loans/collection_report2.html", context)


def get_contract_lines(pk):
    loan = Loan.objects.get(pk=pk)
    lines = loan.lines.all().order_by("date")

    mid_list = int(lines.count() / 2) + 1
    display_lines = []
    for line in range(mid_list):
        left_part = (
            lines[line].date,
            lines[line].principal.quantize(Decimal("1."), rounding=ROUND_DOWN),
        )
        try:
            right_part = (
                lines[line + mid_list].date,
                lines[line + mid_list].principal.quantize(
                    Decimal("1."), rounding=ROUND_DOWN
                ),
            )
        except IndexError:
            # odd number of repayments, the last one is empty, so we make something up to fill the half row
            right_part = ()

        row = left_part + right_part
        display_lines.append(row)

    context = {
        "loan": loan,
        "display_lines": display_lines,
    }
    return context


class PrintLoanContractView(LoginRequiredMixin, View):
    """
    A contract ready to print :)
    """

    def get(self, request, pk):
        return render(request, "loans/print_contract.html", get_contract_lines(pk))


class PrintLoanContractPDFView(LoginRequiredMixin, PDFTemplateView):
    filename = "loan_contract.pdf"
    template_name = "loans/print_contract.html"
    cmd_options = {
        "javascript-delay": 500,
    }

    def get_context_data(self, **kwargs):
        self.filename = "loan_contract_" + str(kwargs["pk"]) + ".pdf"
        context = super(PrintLoanContractPDFView, self).get_context_data(**kwargs)
        # merge the 2 dictionaries in a single one. requires python 3.5
        # see https://treyhunner.com/2016/02/how-to-merge-dictionaries-in-python/
        return {**context, **get_contract_lines(kwargs["pk"])}


@csrf_exempt
def create_renewal_contract(request, pk):
    """
    Simple form to create a renewal loan contract.
    Works with a lot of assumptions:
    - borrower and guarantor are the same as previous loan
    - amount is previous amount + 10000 kyat
    - fee is standardized based on loan amount
    - repayment schedule: same amounts are previous loan, fee paid on disbursement day
    """
    if request.method == "POST":
        try:
            # get request params
            current_loan = Loan.objects.get(pk=pk)
            loan = Loan()
            loan.loan_amount = Decimal(current_loan.loan_amount) + Decimal(10000)
            loan.borrower = current_loan.borrower
            loan.guarantor = current_loan.guarantor
            day = [int(x) for x in request.POST.get("contractDate").split("-")]
            start_date = d(day[0], day[1], day[2])
            loan.normal_repayment_amount = current_loan.normal_repayment_amount
            loan.bullet_repayment_amount = current_loan.bullet_repayment_amount
            loan.calculate_loan_fee()
            loan.save()

            no_of_lines = Loan.get_number_of_repayments(
                loan.loan_amount,
                loan.normal_repayment_amount,
                loan.bullet_repayment_amount,
            )

            for line_no in range(no_of_lines):
                line = RepaymentScheduleLine()
                line.loan = loan
                line.date = start_date + timedelta(days=line_no + 1)
                line.principal = (
                    loan.bullet_repayment_amount
                    if line_no == no_of_lines - 1
                    else loan.normal_repayment_amount
                )
                line.fee = 0
                line.save()
            # for now, we collec the fee on disbursement day
            fee_line = RepaymentScheduleLine()
            fee_line.loan = loan
            fee_line.date = start_date
            fee_line.principal = 0
            fee_line.fee = loan.loan_fee
            fee_line.save()

        except KeyError as e:
            # return JsonResponse({'error': 'Could not parse arguments'})
            raise (e)
        # except Exception as e:
        #    return JsonResponse({'error': '{0}: {1!r}'.format(type(e).__name__, e.args)})
        else:
            return JsonResponse({"result": "ok", "loan_id": loan.pk})
    else:  # non POST requests
        return JsonResponse({"error": "invalid request"})


@login_required
def backend_today_view(request):
    if request.method == "GET":
        # if no agent is selected, select all agent
        try:
            # select just one agent (eg: url/?agent=2)
            agent_pk = request.GET.get("agent")
            agent_list = [Agent.objects.get(pk=agent_pk)]
        except Agent.DoesNotExist:
            agent_list = list(Agent.objects.all())

        # collect data per agent
        data = []
        for agent in agent_list:
            # some agent have None user! Strange!
            today_data = []
            if agent.user is not None:
                # convert to Rest request
                rest_req = Request(request)
                rest_req.user = agent.user
                response = TodayView().get(
                    rest_req, backend_today_request="backend_today_request"
                )
                today_data = response.data["today"]
                # replace borrower id with borrower and loan id with loan
                for row in today_data:
                    row["borrower"] = Borrower.objects.get(pk=row["borrower"])
                    row["loan"] = Loan.objects.get(pk=row["loan"])
            data.append(today_data)

        context = {
            "agent_list": agent_list,
            "today_list": data,
            "showall": request.GET.get("showall", False),
        }

        return render(request, "loans/backend_today_view.html", context)


@login_required
def backend_new_loan_reportview(request):
    if request.method == "GET":
        # if no agent is selected, select all agent
        try:
            # select just one agent (eg: url/?agent=2)
            agent_pk = request.GET.get("agent")
            agent_list = [Agent.objects.get(pk=agent_pk)]
            # agent_list = [Agent.objects.get(pk=40)]
        except Agent.DoesNotExist:
            agent_list = list(Agent.objects.all())

        # collect data per agent
        data = []
        for agent in agent_list:
            # some agent have None user! Strange!
            today_data = []
            if agent.user is not None:
                # convert to Rest request
                rest_req = Request(request)
                rest_req.user = agent.user
                response = TodayView().get(
                    rest_req, backend_today_request="backend_today_request"
                )
                today_data = response.data["today"]
                # replace borrower id with borrower and loan id with loan
                for row in today_data:
                    row["borrower"] = Borrower.objects.get(pk=row["borrower"])
                    row["loan"] = Loan.objects.get(pk=row["loan"])
            data.append(today_data)
        context = {
            "agent_list": agent_list,
            "today_list": data,
            "showall": request.GET.get("showall", False),
        }

        return render(request, "loans/v2_backend_new_loan_reportview.html", context)


@login_required
def disbursement_report(request):
    objects = Disbursement.objects.all()
    context = {
        "disbursements": objects,
    }
    return render(request, "loans/disbursement_report.html", context)


@login_required
def customer_retention_report(request):
    borrower_list = Borrower.objects.all()
    data = []
    for borrower in borrower_list:
        objects = Loan.objects.filter(
            Q(state=LOAN_DISBURSED) | Q(state=LOAN_REPAID), borrower=borrower
        ).order_by("-id")
        loans = 0
        subs = 0
        for obj in objects:
            if obj.is_subscription:
                subs = subs + 1
            else:
                loans = loans + 1
        loan = {
            "borrower": borrower,
            "number_of_loans": objects.count() if objects else 0,
            "loans": loans,
            "subs": subs,
            "last_loan": objects[0].contract_date if objects else 0,
        }
        data.append(loan)

    context = {
        "all_subscriber": data,
    }
    return render(request, "loans/customer_retention_report.html", context)

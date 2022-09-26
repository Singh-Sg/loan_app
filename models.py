import boto3
from datetime import date as d, timedelta, datetime as dt
from decimal import *
from enum import Enum, unique
import logging
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import uuid
from django.conf import settings
from django.contrib.auth.models import User
from jsonfield import JSONField
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db import models, transaction
from django.db.models import Max, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.safestring import mark_safe
from django_fsm import FSMField, RETURN_VALUE, transition
from stdimage.models import StdImageField
from stdimage.utils import UploadToUUID
from borrowers.models import Agent, Borrower
from zw_utils.models import Currency, ZWBaseError
from org.models import MFIBranch
from payments.models import Transfer


class RepaymentBreakdownError(ZWBaseError):
    """
    Error in breaking down a repayment into its components: values don't add up
    """
    pass


class LoanAlreadyRepaidError(ZWBaseError, ValidationError):
    """
    The user is trying to save a Repayment against a Loan that is marked as fully repaid.
    This means something is wrong:
    - the Loan was incorrectly marked as repaid
    - the Repayment is not directed at this Loan
    - something else even weirder
    """
    pass


class RepaymentTooBigError(ZWBaseError, ValidationError):
    """
    The Repayment being saved has a higher amount than what is left to pay on the Loan.
    As such, it cannot be recorded, and some corrective action must be taken, usually correct
    the repayment amount before trying to save again.
    """
    pass


class PosteriorRepaymentAlreadyRecorded(ZWBaseError):
    """
    A Repayment with a date later than the current one has already been recorded in the database.
    This should not happen. Abort everything right away and figure out what is happening.
    """
    pass


class PrincipalOutstandingNegativeError(ZWBaseError):
    pass


class FeeOutstandingNegativeError(ZWBaseError):
    pass


class SubscriptionOutstandingNegativeError(ZWBaseError):
    pass


class InterestOutstandingNegativeError(ZWBaseError):
    pass


class UnsupportedLoanInterestTypeError(ZWBaseError):
    pass


class NoLoanReviewError(ZWBaseError):
    pass


class BorrowerPhotoMissingError(ZWBaseError):
    pass


class PhotoSignatureWithoutPhotoError(ZWBaseError):
    pass


class FeeNotPaidError(ZWBaseError):
    """
    when loan is disbursed, fee should be received first
    """
    pass


class LoanPurpose(models.Model):
    """
    Options to record what a loan will be used for
    """
    description = models.CharField(max_length=100)


# FIXME: we keep this code for now, because somehow makemigrations won't work without it
########################################################################################
# loan status options


@unique
class LoanState(Enum):
    LOAN_REQUEST_DRAFT = 101  # default status when creating the loan in the app
    LOAN_REQUEST_SUBMITTED = 102  # entered once loan request has been POSTed to the server
    LOAN_REQUEST_REJECTED = 103  # loan request rejected by lender. It may come with an alternative loan offer that would have the LOAN_LOAN_REQUEST_APPROVED status
    LOAN_REQUEST_APPROVED = 104  # request approved by the lender, upon entering this status, notify borrower
    LOAN_REQUEST_SIGNED = 105  # entered once server receives contract signature data (photo or whatever)
    DISBURSED = 106  # entered once a disbursement record is saved in the database
    REPAID = 107  # entered when the sum of repayments equals sums to be repaid

    # loan problems codes
    DEFAULTED = 201  # entered according to lender specific logic (xx days late for instance)
    # fraud codes
    LOAN_FRAUD_SUSPECTED = 301  # something suspicious happened (borrower photos don't match, etc...)
    LOAN_FRAUD_CONFIRMED = 302  # investigation confirmed something dubious


########################################################################################

LOAN_REQUEST_DRAFT = 'draft'  # default status when creating the loan in the app
LOAN_REQUEST_SUBMITTED = 'submitted'  # entered once loan request has been POSTed to the server
LOAN_REQUEST_REJECTED = 'rejected'  # loan request rejected by lender. It may come with an alternative loan offer that would have the LOAN_LOAN_REQUEST_APPROVED status
LOAN_REQUEST_APPROVED = 'approved'  # request approved by the lender, upon entering this status, notify borrower
LOAN_REQUEST_SIGNED = 'signed'  # entered once server receives contract signature data (photo or whatever)
LOAN_DISBURSED = 'disbursed'  # entered once a disbursement record is saved in the database
LOAN_REPAID = 'repaid'  # entered when the sum of repayments equals sums to be repaid

# loan problems codes
LOAN_DEFAULTED = 'defaulted'  # entered according to lender specific logic (xx days late for instance)

# fraud codes
LOAN_FRAUD_SUSPECTED = 'LOAN_FRAUD_suspected'  # something suspicious happened (borrower photos don't match, etc...)
LOAN_FRAUD_CONFIRMED = 'LOAN_FRAUD_confirmed'  # investigation confirmed something dubious

LOAN_STATUS_CHOICES = (
    (LOAN_REQUEST_DRAFT, 'Draft request'),
    (LOAN_REQUEST_SUBMITTED, 'Request submitted'),
    (LOAN_REQUEST_REJECTED, 'Request rejected'),
    (LOAN_REQUEST_APPROVED, 'Request approved'),
    (LOAN_REQUEST_SIGNED, 'Request signed by borrower'),
    (LOAN_DISBURSED, 'Loan disbursed'),
    (LOAN_REPAID, 'Loan fully repaid'),
    (LOAN_DEFAULTED, 'Loan defaulted'),
    (LOAN_FRAUD_SUSPECTED, 'Fraud suspected'),
    (LOAN_FRAUD_CONFIRMED, 'Fraud confirmed'),
)


@unique
class LoanInterestTypes(Enum):
    ACTUAL_360 = 0  # declining balance - actual / 360
    ACTUAL_365 = 1  # declining balance - actual / 365
    EQUAL_REPAYMENTS = 2  # declining balance - equal repayments


ACTUAL_360 = 'actual_360'
ACTUAL_365 = 'actual_365'
EQUAL_REPAYMENTS = 'equal_repayments'

LOAN_INTEREST_TYPES_CHOICES = (
    (ACTUAL_360, 'actual_360'),
    (ACTUAL_365, 'actual_365'),
    (EQUAL_REPAYMENTS, 'equal_repayments')
)


@unique
class LoanInterestDuration(Enum):
    MONTHLY = 0
    YEARLY = 1


MONTHLY = 'monthly interest'
YEARLY = 'yearly interest'

LOAN_INTEREST_DURATION_CHOICES = (
    (MONTHLY, 'monthly interest'),
    (YEARLY, 'yearly interest'),
)


@unique
class ReconciliationState(Enum):
    # corresponding repayments or wave money sms have not received yet
    NOT_RECONCILED = 0
    # when repayments reconcile with Wave Money receive SMS by bot
    AUTO_RECONCILED = 1
    # when repayments reconcile with Wave Money receive SMS manually
    MANUAL_RECONCILED = 2
    # time have passed still haven't received repayments or wave money sms
    NEED_MANUAL_RECONCILIATION = 3


# corresponding repayments or wave money sms missing
NOT_RECONCILED = 'not reconciled'
# when repayments reconcile with Wave Money receive SMS by bot
AUTO_RECONCILED = 'auto reconciled'
# when repayments reconcile with Wave Money receive SMS manually
MANUAL_RECONCILED = 'manual reconciled'
# time have passed still haven't received repayments or wave money sms
NEED_MANUAL_RECONCILIATION = 'need manual reconciliation'

RECONCILIATION_STATUS_CHOICES = (
    (NOT_RECONCILED, 'not reconciled'),
    (AUTO_RECONCILED, 'auto reconciled'),
    (MANUAL_RECONCILED, 'manual reconciled'),
    (NEED_MANUAL_RECONCILIATION, 'need manual reconciliation')
)


class Notification(models.Model):
    """
    A simple system to notify Agents about loan activity, like:
    - Loan has been approved/rejected
    - Disbursement is available
    Since they only concern Loans at this point, make them very simple.
    The notifications will be retrieved by the agent via a polling loop for now.
    """
    loan = models.ForeignKey('Loan', on_delete=models.CASCADE)
    # in practice, this will only be LOAN_REQUEST_APPROVED, LOAN_REQUEST_REJECTED or DISBURSED
    new_state = models.CharField(max_length=50, choices=LOAN_STATUS_CHOICES)


class Loan(models.Model):
    """
    A class that represents a loan contract (nano, micro, whatever, they work the same way in the end.)
    """

    class Meta:
        """
        Define some custom permissions that apply to a loan object.
        """
        permissions = (
            ("zw_request_loan", "Can request a new loan"),
            ("zw_approve_loan", "Can approve a loan request"),
            ("zw_disburse_loan", "Can disburse a loan"),
        )

    def __init__(self, *args, **kwargs):
        """
        this is to check subscription total between RepaymentScheduleLine(s) and Repayment(s) in admin
        https://stackoverflow.com/q/13526792/8211573
        """
        super(Loan, self).__init__(*args, **kwargs)
        self.__subscription_total__ = 0

    def generate_contract_number():
        return str(uuid.uuid4().int)[0:12]

    contract_number = models.CharField(max_length=50, blank=True, default=generate_contract_number)
    borrower = models.ForeignKey(Borrower, related_name='loans', on_delete=models.PROTECT)
    guarantor = models.ForeignKey(Borrower, related_name='loans_guaranteed', on_delete=models.PROTECT, blank=True,
                                  null=True)

    # the date on which the MFI approves the contract (which is later than uploaded_at)
    contract_date = models.DateField(default=d.today)
    # the date/time this object was first created, this does not get updated after that
    uploaded_at = models.DateTimeField(auto_now_add=True)

    state = FSMField(
        default=LOAN_REQUEST_DRAFT,
        choices=LOAN_STATUS_CHOICES
    )

    loan_currency = models.ForeignKey(Currency, default=1, on_delete=models.PROTECT)

    loan_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    """ loan interest types """
    loan_interest_type = FSMField(
        default=ACTUAL_360,
        choices=LOAN_INTEREST_TYPES_CHOICES
    )

    """ duration loan_interest_rate is calculated on (monthly or yearly) """
    loan_interest_duration = FSMField(
        default=MONTHLY,
        choices=LOAN_INTEREST_DURATION_CHOICES
    )

    """ loan_interest_rate per loan_interest_days. 2.5% should be entered as 2.5 """
    loan_interest_rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text=" % interest per monthly or yearly "
    )

    def calculate_loan_fee(self):
        """
        Calculate a fee for the loan given it's current settings.
        """
        fee_table = {
            '20000': 400,
            '30000': 600,
            '40000': 800,
            '50000': 1000,
            '60000': 1200,
            '70000': 1400,
            '80000': 1600,
            '90000': 1800,
            '100000': 2000,
        }
        self.loan_fee = fee_table[str(int(self.loan_amount))]
        self.late_penalty_fee = self.loan_fee

    loan_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    late_penalty_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    late_penalty_per_x_days = models.IntegerField(default=7)
    late_penalty_max_days = models.IntegerField(default=70)
    prepayment_penalty = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    """must use this field when loan_interest_type is EQUAL_REPAYMENTS"""
    number_of_repayments = models.SmallIntegerField(default=0)

    normal_repayment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    bullet_repayment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    loan_contract_photo = models.ImageField(blank=True, upload_to='uploads/loan_contract/%Y/%m/%d/')

    @property
    def days_late(self):
        return self.get_delay()

    @property
    def current_delay(self):
        """
        Convenience function for current_delay_at(today)
        """
        return self.current_delay_at(d.today())

    def current_delay_at(self, date):
        """
        The number of days with outstanding is > 0 for that day, since last time the outstanding was less than 0

        There are two cases for this function
        case 1. ongoing day (or) today
        If this function is called for today, planned repayment for today should not be considered.
        case 2. past day
        If this function is called for one of the past days(not today), planned repayment for that day should be considered.

        pseudo code of the program
        A.for a given day(that day), checks if actual repayments covers missing planned repayments
        due = (planned repayments with date < that day) - (actual repayments with date <= that day)
        if due <= 0:
            if it is not today:
                go to B
            program end
        if due > 0,
            if it is not today:
                delay + 1
            go to A for previous day

        B.consider that day planned repayments
        due from A += planned repayments for that day
        if due > 0:
            delay + 1
        program end
        """

        def reverse_daterange(start, end):
            curr = end
            while curr >= start:
                yield curr
                curr += timedelta(days=-1)

        delay = 0

        lines = self.lines.filter(date__lte=date)
        repayments = self.repayments.filter(date__lte=date)

        start_date = min([date] + [l.date for l in lines] + [r.date for r in repayments])

        for day in reverse_daterange(start_date, date):
            due = 0
            # for a given day, do not consider planned repayment at first
            for line in [l for l in lines if l.date <= day - timedelta(days=1)]:
                due += getattr(line, 'principal', 0)
            for r in [r for r in repayments if r.date <= day]:
                due -= getattr(r, 'principal', 0)
            # delay only if it is not today
            if due > 0 and day != d.today():
                delay += 1
            elif due <= 0:
                # if it is not today, check planned repayment for that day
                # FIXME: this may have problem with timezone
                if day != d.today():
                    for line in [l for l in lines if l.date == day]:
                        due += getattr(line, 'principal', 0)
                    if due > 0:
                        delay += 1
                return delay
        # there is no repayments for this loan
        return delay

    @property
    def next_loan_max_amount(self):
        if self.loan_amount == 0:
            return 20000   # if this is a pure subscription loan, then consider the next one as a first loan
        else:
            return self.loan_amount + 10000

    @property
    def contract_due_date(self):
        try:
            return self.lines.order_by('-date')[0].date
        except:
            return None

    @property
    def latest_repayment_date(self):
        """
        return the latest repayment received for this loan. This is just a date comparison,
        it does not imply anything on the status of the loan.
        """
        try:
            return self.repayments.order_by('-date')[0].date
        except IndexError:
            return None

    """Set to the date of the last payment, that closes the loan. None if the loan is active/open"""

    def validate_repaid_on_not_in_future(value):
        if value > d.today():
            raise ValidationError(
                'A repayment date cannot be in the future.',
                params={'value': value},
            )

    repaid_on = models.DateField(
        blank=True,
        null=True,
        help_text='Automatically set to the date of the repayment closing the loan. Not set for open loans.',
        validators=[validate_repaid_on_not_in_future],
    )

    # this is calculated automatically upon saving the loan details
    effective_interest_rate = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True, editable=False)

    """temp field until we sort things out"""
    pilot = models.CharField(max_length=20, blank=True)

    purpose = models.ForeignKey(LoanPurpose, blank=True, null=True, default=None, on_delete=models.PROTECT)

    # read-only fields, calculated each time a repayment is saved
    @property
    def principal_outstanding(self):
        """
        return how much of the principal is left to repay at the time of the function call
        """
        principal_repaid = self.repayments.aggregate(paid=Sum('principal'))['paid'] or 0
        po = self.loan_amount - principal_repaid
        if po < 0:
            raise PrincipalOutstandingNegativeError(self)
        return po

    def principal_outstanding_at(self, day):
        """
        return how much of the principal is left to repay at the end of the `day`.
        this function is intended to use for PortfolioStats calculation.
        if this function is called for today, this will probably the same as the function 'principal_outstanding' above
        """
        principal_repaid = self.repayments.filter(date__lte=day).aggregate(paid=Sum('principal'))['paid'] or 0
        po = self.loan_amount - principal_repaid
        if po < 0:
            raise PrincipalOutstandingNegativeError(self)
        return po

    @property
    def fee_outstanding(self):
        """
        return how much of the fee is left to repay at the time of the function call
        """
        fee_paid = self.repayments.aggregate(fee_paid=Sum('fee'))['fee_paid'] or 0
        fo = self.loan_fee - fee_paid
        if fo < 0:
            raise FeeOutstandingNegativeError(self)
        return fo

    @property
    def subscription_outstanding(self):
        """
        return how much of the subscription is left to repay at the time of the function call
        """
        subscription_paid = self.repayments.aggregate(subscription_paid=Sum('subscription'))['subscription_paid'] or 0
        total_subscription_due = self.lines.aggregate(subscription_due=Sum('subscription'))['subscription_due'] or 0
        so = total_subscription_due - subscription_paid
        if so < 0:
            raise SubscriptionOutstandingNegativeError(self)
        return so

    @property
    def is_subscription(self):
        total_subscription_due = self.lines.aggregate(subscription_due=Sum('subscription'))['subscription_due'] or 0
        return total_subscription_due != 0

    @property
    def sum_of_subscription(self):
        total_subscription_due = self.lines.aggregate(subscription_sum=Sum('subscription'))['subscription_sum']
        return total_subscription_due

    # interest related methods
    # Warning: always call update_attributes_for_lines on the start of the day for interest of future lines to correct
    # In real server, this daily call is done in celery tasks

    # interest calculation methods
    @classmethod
    def _calculate_interest_for_actual_360_or_365(cls, loan_interest_type, balance, interest_rate, interest_duration,
                                                  interest_period):
        """
        This method is only for ACTUAL_360 and ACTUAL_365
        calculate interest via formula depending on self.loan_interest_type
        :param loan_interst_type:
        :param balance: the amount of money left (principal_outstanding)
        :param interest_rate: rate of interest
        :param interest_duration: duration, interest is calculated on (MONTHLY or YEARLY)
        :param interest_period: number of days between interest calculations
                                (for e.g if interest is calculated every 5 days, then this is 5)
        :return: round(integer)
        """
        # convert to Decimal since Decimal give more precise value
        balance = Decimal(balance)
        interest_rate = Decimal(interest_rate)
        interest_period = Decimal(interest_period)
        # if interest is monthly, convert to yearly first
        if interest_duration == MONTHLY:
            interest_rate *= Decimal(12)
        # actual calculation
        if loan_interest_type == ACTUAL_360:
            return round(balance * (interest_period / Decimal(360)) * (interest_rate / Decimal(100)))
        elif loan_interest_type == ACTUAL_365:
            return round(balance * (interest_period / Decimal(365)) * (interest_rate / Decimal(100)))
        else:
            raise UnsupportedLoanInterestTypeError(cls)

    @classmethod
    def _calculate_components_for_equal_repayments(cls, base_balance, balance, interest_rate, interest_duration,
                                                   interest_period, number_of_repayments,
                                                   number_of_periods_since_contract, day_of_restart=0):
        # FIXME: this will raise DivisdeByZero error if interest_rate is 0. Cannot be fixed by validator for now since validator need to be classmethod
        """
        This method is for loan_interest_type EQUAL_REPAYMENTS
        :param base_balance: the amount of balance where equal repayment and remaining balance are calculated
                            in the case of perfect repayments, this is equal to loan_amount
                            in the case of early and late repayment, this should be changed to remaining balance on the day of early or late repayment
        :param balance:
        :param interest_rate:
        :param interest_duration: MONTHLY or YEARLY
        :param interest_period: the number of days between interest calculation
        :param number_of_repayments: number of repayments to be done
        :param number_of_periods_since_contract:
        :param day_of_restart: in the case of early or late repayment, every future calculation should be done on data on that day. That day is day_of_restart
                               for perfect repayment cases, set it to 0. In other words, the number of period difference between
                               the last day when repayment is early or late and contract date
        e.g for early(or)late repayment
        In daily interest, let's say loan contract is 19th and should be repaid starting on 20th.
        Borrower repay early(or)late on 22th. Then day of restart is 22th - 19th = 3 and base balance is
        remaining balance on 22th.
        :return: dictionary {'repayment': int, 'principal': int, 'interset': int, 'balance': int}
                repayment - amount of equal repayment which is principal + interest
                balance - remaining balance if repaid
        """
        # convert everything to decimal
        base_balance = Decimal(base_balance)
        balance = Decimal(balance)
        interest_rate = Decimal(interest_rate)
        interest_period = Decimal(interest_period)
        number_of_repayments = Decimal(number_of_repayments)
        number_of_periods_since_contract = Decimal(number_of_periods_since_contract)
        day_of_restart = Decimal(day_of_restart)

        # change interest to yearly if monthly
        if interest_duration == MONTHLY:
            interest_rate *= 12

        # some variables to make calculation more readable
        interest_rate_per_period = (interest_rate / ((Decimal(365) * Decimal(100)))) * interest_period
        one_plus_i_power_n = (Decimal(1) + interest_rate_per_period) ** (number_of_repayments - day_of_restart)
        one_plus_i_power_p = (Decimal(1) + interest_rate_per_period) ** (
            number_of_periods_since_contract - day_of_restart)

        # calculation start!
        equal_repayment_amount = (base_balance * (interest_rate_per_period * one_plus_i_power_n)) / (
            one_plus_i_power_n - Decimal(1))
        remaining_balance = (base_balance * (one_plus_i_power_n - one_plus_i_power_p)) / (
            one_plus_i_power_n - Decimal(1))
        principal = balance - remaining_balance
        interest = equal_repayment_amount - principal

        return {'repayment': round(equal_repayment_amount), 'principal': round(principal), 'interest': round(interest),
                'balance': round(remaining_balance)}

    def calculate_interest(self, balance, number_of_periods_since_contract=1, base_balance=0, day_of_restart=0):
        """
        This method is to calculate interest quickly with a bunch of assumptions
        To have more control, use _calculate_interest which is for ACTUAL_360 and ACTUAL_365
        for EQUAL_REPAYMENTS, use _calculate_components_for_equal_repayments
        :param balance:
        The following parameters are only required for EQUAL_REPAYMENTS
        Look at formula for better understanding
        :param number_of_periods_since_contract:
        :param base_balance: In perfect cases, this is equal to loan amount.
                             In early or late cases, this is balance from the time early or late repayment occurred.
        :param day_of_restart: this parameter is only required in the case of equal repayments with early or late repayments.
                               the number of periods difference between the last day when repayment is early or late and contract date.
        e.g for early(or)late repayment for loan interest type equal repayments.
        In daily interest, let's say loan contract is 19th and should be repaid starting on 20th.
        Borrower repay early(or)late on 22th. Then day of restart is 22th - 19th = 3 and base balance is
        remaining balance on 22th.
        """
        # In our case, interest period is daily and set to 1
        if self.loan_interest_type == ACTUAL_360:
            return self._calculate_interest_for_actual_360_or_365(ACTUAL_360, balance, self.loan_interest_rate,
                                                                  self.loan_interest_duration, 1)
        elif self.loan_interest_type == ACTUAL_365:
            return self._calculate_interest_for_actual_360_or_365(ACTUAL_365, balance, self.loan_interest_rate,
                                                                  self.loan_interest_duration, 1)
        elif self.loan_interest_type == EQUAL_REPAYMENTS and day_of_restart == 0:
            base_balance = self.loan_amount
            return self._calculate_components_for_equal_repayments(base_balance, balance, self.loan_interest_rate,
                                                                   self.loan_interest_duration, 1,
                                                                   self.number_of_repayments,
                                                                   number_of_periods_since_contract)['interest']
        elif self.loan_interest_type == EQUAL_REPAYMENTS and day_of_restart > 0:
            return self._calculate_components_for_equal_repayments(base_balance, balance, self.loan_interest_rate,
                                                                   self.loan_interest_duration, 1,
                                                                   self.number_of_repayments,
                                                                   number_of_periods_since_contract, day_of_restart)[
                'interest']
        else:
            raise UnsupportedLoanInterestTypeError(self)

    @classmethod
    def calculate_equal_repayment_amount_for_loan_initialization(cls, loan_amount, interest_rate, number_of_repayments,
                                                                 interest_duration=MONTHLY, interest_period=1):
        """
        For creating loan, normal repayment amount need to be known beforehand.
        This method calculate normal repayment amount for loan_interest_type EQUAL_REPAYMENTS
        Only use this method when loan_interest_type is EQUAL_REPAYMENTS
        Use this method to set normal repayment amount when loan_interest_type is EQUAL_REPAYMENTS
        """
        return \
            cls._calculate_components_for_equal_repayments(loan_amount, loan_amount, interest_rate, interest_duration,
                                                           interest_period, number_of_repayments, 1)['repayment']

    # Repayment Schedule Line updating methods
    def _update_interest_for_today_and_future_lines_for_actual_360_or_365(self):
        """
        This method is to update interest of today and future lines when loan_interest_type is ACTUAL_360 or ACTUAL_365
        For normal repayment, interest of future lines stay the same
        For early repayment, interest of future lines decrease
        For late repayment, interest of future lines increase

        How the function work
        Calculate today interest depending on yesterday_principal_outstanding
        Then interest of future lines are calculated depending on today data
        p.s This function update immediately (unlike update function for equal repayments) when repayment is received
        """
        # yesterday principal outstanding which may be used to calculate today interest
        principal_repaid_until_yesterday = self.repayments.filter(
            date__lt=d.today()
        ).aggregate(
            paid=Sum('principal')
        )['paid'] or 0
        yesterday_principal_outstanding = self.loan_amount - principal_repaid_until_yesterday
        if yesterday_principal_outstanding < 0:
            raise PrincipalOutstandingNegativeError(self)

        # some variables that will be useful in updating future lines
        today_principal_outstanding = self.principal_outstanding
        today_due_principal = 0

        # there is no line on contract date and after due date
        # calculate interest for today depending on yesterday principal outstanding
        if self.uploaded_at.date() < d.today() <= self.contract_due_date:
            today_line = self.lines.get(date=d.today())
            today_line.interest = self.calculate_interest(yesterday_principal_outstanding)
            today_line.save()
            # keep today data for following days
            today_principal_outstanding = self.principal_outstanding
            today_due_principal = self.amount_due_for_date(today_line.date)['principal']

        # update future lines
        for line in self.lines.filter(date__gt=d.today()).order_by('date'):
            remaining_balance = today_principal_outstanding - today_due_principal
            line.interest = self.calculate_interest(remaining_balance)
            line.save()
            # keep today data for following days
            today_due_principal = self.amount_due_for_date(line.date)['principal']

    def _update_principal_and_interest_for_lines_for_equal_repayments(self):
        """
        This method is for loan_interest_type EQUAL_REPAYMENTS
        Update principal and interest of yesterday, today and future lines
        For normal repayment, principal and interest of future lines stay the same
        For early or late repayment, principal and interest of lines change

        How the function work
        First, after one day after first repayment, (So, that past repayment data exists), check if past repayments are paid.
        If yes, lines do not need to be updated, else, update yesterday data
        Then, calculate today data depending on yesterday principal outstanding
        Lastly, update future lines according to today data
        p.s This function do not update immediately (unlike update function for actual) when repayment is received. Need to call at tomorrow to update lines.
        """
        # if yesterday repayment and schedule repayment matched then we do not need to update
        yesterday = d.today() - timedelta(days=1)
        # make sure yesterday repayment and line exist
        if (self.uploaded_at.date() + timedelta(days=1)) < d.today() <= self.contract_due_date:
            past_line_principal = self.lines.filter(date__lt=d.today()).aggregate(paid=Sum('principal'))['paid'] or 0
            past_repaid_principal = self.repayments.filter(date__lt=d.today()).aggregate(paid=Sum('principal'))['paid'] or 0
            # if past repayments are paid do not update lines
            past_principal_offset = past_line_principal - past_repaid_principal
            if past_principal_offset == 0:
                return
            else:  # if past repayments is more or less than normal amount, then update yesterday principal. Otherwise, it will cause error in repayment.breakdown
                yesterday_repaid_principal = self.repayments.filter(
                    date=yesterday
                ).aggregate(
                    paid=Sum('principal')
                )['paid'] or 0
                yesterday_line = self.lines.get(date=yesterday)
                yesterday_line.principal = yesterday_repaid_principal
                yesterday_line.save()

        # yesterday principal outstanding which may be used to calculate today interest
        principal_repaid_until_yesterday = self.repayments.filter(date__lt=d.today()).aggregate(paid=Sum('principal'))['paid'] or 0
        yesterday_principal_outstanding = self.loan_amount - principal_repaid_until_yesterday
        if yesterday_principal_outstanding < 0:
            raise PrincipalOutstandingNegativeError(self)

        # some variables that will be useful in updating future lines
        today_principal_outstanding = self.principal_outstanding
        day_of_restart = 0

        # there is no line on contract date and after due date
        # calculate interest for today depending on yesterday principal outstanding
        if self.uploaded_at.date() < d.today() <= self.contract_due_date:
            today_line = self.lines.get(date=d.today())
            day_of_restart = (yesterday - self.uploaded_at.date()).days
            number_of_periods_between_tdy_and_contract = (d.today() - self.uploaded_at.date()).days
            components = self._calculate_components_for_equal_repayments(yesterday_principal_outstanding,
                                                                         yesterday_principal_outstanding,
                                                                         self.loan_interest_rate,
                                                                         self.loan_interest_duration, 1,
                                                                         self.number_of_repayments,
                                                                         number_of_periods_between_tdy_and_contract,
                                                                         day_of_restart)
            today_line.principal = components['principal']
            today_line.interest = components['interest']
            today_line.save()
            # keep today data for following days
            today_principal_outstanding = components['balance']

        # update future lines
        for line in self.lines.filter(date__gt=d.today()).order_by('date'):
            number_of_periods_between_line_and_contract = (line.date - self.uploaded_at.date()).days
            components = self._calculate_components_for_equal_repayments(yesterday_principal_outstanding,
                                                                         today_principal_outstanding,
                                                                         self.loan_interest_rate,
                                                                         self.loan_interest_duration, 1,
                                                                         self.number_of_repayments,
                                                                         number_of_periods_between_line_and_contract,
                                                                         day_of_restart)
            line.principal = components['principal']
            line.interest = components['interest']
            line.save()
            # keep today data for following days
            today_principal_outstanding = components['balance']

    def update_attributes_for_lines(self):
        """
        update LoanRepaymentScheduleLine of Loan
        """
        if self.loan_interest_type == ACTUAL_360 or self.loan_interest_type == ACTUAL_365:
            self._update_interest_for_today_and_future_lines_for_actual_360_or_365()
        elif self.loan_interest_type == EQUAL_REPAYMENTS:
            self._update_principal_and_interest_for_lines_for_equal_repayments()
        else:
            raise UnsupportedLoanInterestTypeError(self)

    @property
    def interest_outstanding(self):
        """
        return how much interest is left to repay at the time of the function call
        """
        # force 0 for now
        return 0
        # total interest need to repay
        interest_total = self.lines.filter(date__lte=d.today()).aggregate(amount=Sum('interest'))['amount'] or 0
        # total interest being paid
        interest_repaid = self.repayments.aggregate(paid=Sum('interest'))['paid'] or 0
        io = interest_total - interest_repaid
        if io < 0:
            raise InterestOutstandingNegativeError(self)
        return io

    @property
    def penalty_outstanding(self):
        """
        return how much penalty fee is left to repay at the time of the function call
        """
        return 0  # until we define a proper calculation

    @property
    def total_outstanding(self):
        q = self.repayments.aggregate(principal_paid=Sum('principal'), fee_paid=Sum('fee'))

        principal_paid = q['principal_paid'] or 0
        po = self.loan_amount - principal_paid
        if po < 0:
            raise PrincipalOutstandingNegativeError(self)

        fee_paid = q['fee_paid'] or 0
        fo = self.loan_fee - fee_paid
        if fo < 0:
            raise FeeOutstandingNegativeError(self)

        try:
            return po + fo + self.interest_outstanding + self.penalty_outstanding + self.subscription_outstanding
        except SubscriptionOutstandingNegativeError as e:
            # FIXME: temp hack to allow loans to be shown
            # report this in error report to help pinpoint where the problem is coming from
            # ideally, we should prevent users from creating this situation in the first place
            # possibly by preventing manual input of repayment breakdowns
            logger = logging.getLogger('root')
            logger.error('Invalid subscription schedule (outstanding set to -1)', exc_info=True, extra={
                'signature': self,
                'exception': e,
            })
            return -1

    # answer to "do you expect your sales to grow (with this loan)?"
    # set to null if not applicable
    expect_sales_growth = models.NullBooleanField(default=None)

    comments = models.TextField(blank=True)

    # Disbursement request stuff, until we figure out something a bit cleaner
    # disbursement = JSONField(blank=True, null=True)
    disbursement = models.ForeignKey('Disbursement', related_name='loans_disbursed', null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return str(self.borrower) + ' | ' + str(self.loan_amount) + ' | ' + str(self.uploaded_at.date())

    def get_breakdown_order(self):
        """
        return an ordered list of components repayment priorities, eg:
        ['penalty', 'fee', 'interest', 'principal'] means components are prioritised
        in that order (penalty first) when receiving money.
        """
        return ['penalty', 'fee', 'interest', 'subscription', 'principal']

    def amount_due_for_date(self, date=d.today()):
        """
        return a dict {'principal': xx, 'fee', yy, ... } of the amounts due on a specific date.
        The amounts due include amounts due on `date` plus all amounts due earlier and not repaid.
        """
        outstanding = {}
        for c in self.get_breakdown_order():
            outstanding[c] = 0

        # if loan is already repaid, no money is due
        if self.repaid_on is not None:
            return outstanding

        # if loan has not been disbursed yet, add lines of fees
        # even if due in the future
        if self.state in [LOAN_REQUEST_APPROVED, LOAN_REQUEST_SUBMITTED]:
            lines = self.lines.filter(fee__gt=0)
            repayments = self.repayments.filter(fee__gt=0)
        else:
            # for loans already disbursed, add all backlog of money due
            lines = self.lines.filter(date__lte=date)
            repayments = self.repayments.filter(date__lte=date)

        for line in lines:
            for c in self.get_breakdown_order():
                outstanding[c] += getattr(line, c, 0)

        for r in repayments:
            for c in self.get_breakdown_order():
                outstanding[c] -= getattr(r, c, 0)

        for c in self.get_breakdown_order():
            if outstanding[c] < 0:
                outstanding[c] = 0

        return outstanding

    def total_amount_due_for_date(self, date=d.today()):
        """
        return the total amount due on `date`, ie: the sum of all components
        """
        o = self.amount_due_for_date(date)
        return sum(o.values())

    def close_if_fully_repaid(self, repayment):
        """
        Set Loan.repaid_on to the repayment.date if this repayment finishes repayment of the Loan.
        Return True if this call closed the loan, False otherwise.
        """
        for component in self.get_breakdown_order():
            component_name = component + '_outstanding'
            if getattr(self, component_name) > 0:
                return False
        # all loan components have been fully repaid, close it!
        self.repaid_on = self.repayments.aggregate(last_date=Max('date'))['last_date']
        # change the loan.state using a transition
        self.mark_closed()
        self.save()
        return True

    def get_delay(self):
        """
        Calculate how many days late a borrower is for this loan.
        1 day late is counted for each day where the outstanding amount is >0 at end of day,
        for all scheduled days before today.
        """

        def reverse_daterange(start, end):
            curr = end
            while curr >= start:
                yield curr
                curr += timedelta(days=-1)

        end_date = d.today() + timedelta(days=-1)

        # FIXME: we should refactor this and current_delay_at as it's the same code
        # and calculate both values in a single pass to save db queries

        loan_lines = self.lines.filter(date__lte=end_date).values('date', 'principal')
        repayments = self.repayments.all().values('date', 'principal')  # take them all as there are no repayments in the future
        delay = 0
        if len(loan_lines):
            start_date = min([l['date'] for l in loan_lines])
        else:
            start_date = end_date

        if len(repayments):
            # min() raises an exception on an empty list
            start_date = min(start_date, min([r['date'] for r in repayments]))

        for day in reverse_daterange(start_date, end_date):
            to_repaid = sum([l['principal'] for l in loan_lines if l['date'] <= day])
            repaid = sum([r['principal'] for r in repayments if r['date'] <= day])
            delay += ((to_repaid - repaid) > 0)

        return delay

    @property
    def next_disbursement_date(self):
        """
        Return the earliest possible disbursement date for the loan,
        defined as Max(contract_due_date, latest_repayment) + delay + 1
        """
        try:
            base_date = max(self.contract_due_date, self.latest_repayment_date)
        except TypeError:
            # no repayment recorded yet
            base_date = self.contract_due_date
        return base_date + timedelta(days=self.get_delay() + 1)

    def total_repaid(self):
        """return the total amount repaid on this loan so far"""
        return sum(r.amount for r in self.repayments.all())

    @classmethod
    def get_outstanding_loans_for_date(cls, date=d.today(), agent=None):
        """
        Return a list of all loans that have a payment due on date
        """
        line_qs = RepaymentScheduleLine.objects.filter(
            date=date
        ).order_by(
            'loan__borrower__name_en'
        )
        loans = []
        for l in line_qs:
            loans.append(l.loan)
        return loans

    @classmethod
    def get_number_of_repayments(cls, loan_amount, normal_repayment_amount, bullet_repayment_amount):
        """
        calculate the number of repayments given the above parameters. classmethod since the object
        probably doesn't exist when we run this.
        """
        try:
            # FIXME: something very weird involving decimal.DivisionUndefined happened here
            # hence the strange code. Haven't managed to reproduce, so leaving it as such for now
            r = (loan_amount - bullet_repayment_amount) / normal_repayment_amount
        except:
            r = 0
        return int(r) + 1

    # Loan.state transitions
    @transition(field=state, source=LOAN_REQUEST_DRAFT, target=LOAN_REQUEST_SUBMITTED)
    def submit_request(self):
        """
        Do nothing for now, just a placeholder
        """
        pass

    @transition(
        field=state,
        source=LOAN_REQUEST_SIGNED,
        target=RETURN_VALUE(LOAN_REQUEST_APPROVED, LOAN_REQUEST_REJECTED)
    )
    def approve_request(self, review=None):
        """
        Approve the loan request. This changes the state, and generates the required notifications,
        and so on. It will be called from the .save() method of LoanRequestApproval.
        """
        # if there is no review object, abort the transition
        if review is None:
            raise NoLoanReviewError('A loan cannot be approved or rejected without a LoanRequestApproval object')

        if review.approved:
            new_state = LOAN_REQUEST_APPROVED
            # notify the agent that the loan has been approved
        else:
            new_state = LOAN_REQUEST_REJECTED

        notif = Notification(
            loan=self,
            new_state=new_state
        )
        notif.save()
        return new_state

    @transition(
        field=state,
        source=LOAN_REQUEST_SUBMITTED,
        target=RETURN_VALUE(LOAN_REQUEST_SIGNED, LOAN_FRAUD_SUSPECTED)
    )
    def sign_loan(self, signature=None):
        """
        The borrower must sign their approved loan request before the loan can be disbursed.
        If anything looks fishy, the loan is marked as suspected fraudulent, for manual review.
        """
        if signature is None:
            # FIXME: unresolved reference `NoLoanSignatureError`
            raise NoLoanSignatureError('A loan cannot be signed without a Signature object')

        if signature.is_valid:
            new_state = LOAN_REQUEST_SIGNED
        else:
            new_state = LOAN_FRAUD_SUSPECTED
        # TODO: trigger disbursements from here
        return new_state

    @transition(field=state, source=LOAN_DISBURSED, target=LOAN_REPAID)
    def mark_closed(self):
        """
        Do nothing for now, just a placeholder
        """
        pass

    @transition(field=state, source=LOAN_REQUEST_APPROVED, target=LOAN_DISBURSED)
    def disburse_loan(self):
        """
        Do nothing for now, just a placeholder
        """
        pass
        # Loan can be disbursed after receiving fee
        # if self.fee_outstanding > 0:
        #     raise FeeNotPaidError('Fee not received yet')


class RepaymentScheduleLine(models.Model):
    """
    A planned repayment for a loan contract. NOT THE ACTUAL REPAYMENT, which is under Repayment.
    """

    # the loan contract this line is related to
    loan = models.ForeignKey(Loan, related_name='lines', on_delete=models.CASCADE)

    # the date payment is expected
    date = models.DateField()

    principal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0))
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0))
    """ use loan method set_current_max_interest_for_future_lines to set this field """
    interest = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0))
    penalty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0))
    subscription = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0))
    note = models.TextField(blank=True)

    def __str__(self):
        return str(self.loan) + ' - ' + str(self.date) + ': ' + 'principal:' + str(self.principal) + ' interest:' + str(
            self.interest) + ' fee:' + str(self.fee)


class ReasonForDelayedRepayment(models.Model):
    """
    Reasons for delayed repayments.
    """
    reason_en = models.CharField(max_length=50, blank=True)
    reason_mm = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return str(self.pk) + ' - ' + str(self.reason_en)


class Reconciliation(models.Model):
    """
    intermediary model for WaveMoneyReceiveSMS and loans.models.Repayment
    """
    reconciled_at = models.DateTimeField(default=timezone.now)

    """ The user who made the reconciliation. For AUTO_RECONCILED, it should be 'reconciliation_bot' """
    reconciled_by = models.ForeignKey(User, on_delete=models.CASCADE)

    def __str__(self):
        su = "None"
        if self.superusertolenderpayment_set.count() > 0:
            su = self.superusertolenderpayment_set.first().super_user.name
        return str(su) + ' ' + str(self.reconciled_at)


class Repayment(models.Model):
    """
    An actual repayment, ie: money being paid back by the borrower, whether on time or not.
    """

    loan = models.ForeignKey(Loan, related_name='repayments', on_delete=models.CASCADE)

    # the date the money was actually repaid
    date = models.DateField()

    """total amount paid in"""
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    """amount of fee taken from this repayment"""
    fee = models.DecimalField(max_digits=12, decimal_places=2)

    """amount of penalty fee taken from this repayment"""
    penalty = models.DecimalField(max_digits=12, decimal_places=2)

    """amount of interest taken from this repayment"""
    interest = models.DecimalField(max_digits=12, decimal_places=2)

    """amount of principal taken from this repayment"""
    principal = models.DecimalField(max_digits=12, decimal_places=2)

    """amount of subscription taken from this repayment"""
    subscription = models.DecimalField(max_digits=12, decimal_places=2)

    """timestamp for when the repayment was recorded on the client side"""
    recorded_at = models.DateTimeField(default=timezone.now, help_text='When the agent saved the repayment in the app')

    """
    timestamp for when the repayment was uploaded to the server from the client.
    For repayments input from admin, recorded_at and uploaded_at are the same.
    """
    uploaded_at = models.DateTimeField(auto_now_add=True, help_text='When the app uploaded this to the server')

    """keep track of the user who recorded this"""
    recorded_by = models.ForeignKey(User, default=1, on_delete=models.PROTECT)

    """reason for delayed repayment (None if not delayed)"""
    reason_for_delay = models.ForeignKey(ReasonForDelayedRepayment, blank=True, null=True, on_delete=models.PROTECT)

    """foreign key to reconciliation table to connect with WaveMoneyReceiveSMS"""
    reconciliation = models.ForeignKey(Reconciliation, blank=True, null=True, on_delete=models.PROTECT, related_name='repayment_list')

    """reconciliation with WaveMoneyReceiveSMS"""
    reconciliation_status = FSMField(
        default=NOT_RECONCILED,
        choices=RECONCILIATION_STATUS_CHOICES
    )

    """set this value when superuser send money to lender"""
    superuser_to_lender_payment = models.ForeignKey('SuperUsertoLenderPayment', blank=True, null=True, on_delete=models.PROTECT)
    note = models.TextField(blank=True)

    @transition(
        field=reconciliation_status,
        source=[NOT_RECONCILED, NEED_MANUAL_RECONCILIATION],
        target=RETURN_VALUE(AUTO_RECONCILED, MANUAL_RECONCILED)
    )
    def set_reconciled(self, intermediary, method):
        self.reconciliation_status = method
        self.reconciliation = intermediary
        self.save(no_checks=True)
        return method

    @transition(
        field=reconciliation_status,
        source=NOT_RECONCILED,
        target=NEED_MANUAL_RECONCILIATION
    )
    def set_need_manual_reconciliation(self):
        self.reconciliation_status = NEED_MANUAL_RECONCILIATION
        self.save(no_checks=True)

    def __str__(self):
        return str(self.date) + ': ' + str(self.amount) + ' (pk=' + str(self.pk) + ')' + ' p: ' + str(
            self.principal) + ' f: ' + str(self.fee) + ' i: ' + str(self.interest)

    def breakdown(self, force_recalc=False):
        """
        breakdown the repayment into its components (principal, fee, interest, penalty)
        and populate the various properties of the object. This does not save anything!
        force_recalc must be set to True to act on a Repayment that has already been saved.
        """

        amount_left = self.amount

        if self.pk is not None and not force_recalc:
            # abort! we have already broken down this payment
            return

        if force_recalc:
            # clear the current repayment breakdown values as they are wrong
            for c in self.loan.get_breakdown_order():
                setattr(self, c, 0)

        # get all scheduled repayments up to date of payment
        past_loan_lines = self.loan.lines.filter(date__lte=self.date)
        past_repayments = self.loan.repayments.filter(date__lte=self.date).exclude(id=self.id)
        # consume the repayment on past schedule
        for component in self.loan.get_breakdown_order():
            component_total_to_pay = sum(getattr(l, component) for l in past_loan_lines)
            component_repaid = sum(getattr(r, component) for r in past_repayments)

            debt = component_total_to_pay - component_repaid
            # debt > 0 mean some past repayments are not repaid yet
            # debt == 0 mean all past repayments are repaid
            # debt < 0 mean even future repayments are repaid
            if debt >= 0:
                value = min(debt, amount_left)
                amount_left -= value
                setattr(self, component, value)
            else:
                # set it zero for now, the code below for future consumption will take care of this
                setattr(self, component, 0)

        # now if any money is left in the repayment, consume it on future scheduled lines, one by one
        # priority in the future works differently than in the past lines:
        # we need to consume each line entirely before moving to the next one
        if amount_left > 0:
            future_loan_lines = self.loan.lines.filter(date__gt=self.date).order_by('date')
            for line in future_loan_lines:
                breakdown_order = self.loan.get_breakdown_order()
                # FIXME: maybe principal is the only one that is need to be considered in future
                breakdown_order.remove('interest')  # future interest do not need to be considered
                for component in breakdown_order:
                    current_value = getattr(self, component)
                    increment = min(getattr(line, component), amount_left)
                    amount_left -= increment
                    assert value >= 0, '{} is negative due to the future: {}'.format(component, value)
                    setattr(self, component, current_value + increment)
                if amount_left <= 0:
                    break

        # verify that our math is correct at line level
        if not self.amount == sum(getattr(self, component) for component in self.loan.get_breakdown_order()):
            raise RepaymentBreakdownError(self)

    def save(self, *args, update_posterior_repayments=True, no_checks=False, **kwargs):
        """
        Override default behaviour to automatically breakdown the repayment into its
        fee, interest and principal components.
        To save, we need the loan this repayment is attached to, we'll figure out the scheduled_repayment
        in here.
        NOTE: be careful around this function and breakdown(), as it has a LOT of side effects in the database when
        updating future repayments. It is best to `repayment.refresh_from_db()` after it to make sure
        the data is correct.

        TODO: the `update_posterior_repayments` kwarg is used to decide whether the save()
        needs to trigger updating/saving "sibling" repayments (of the same loan).
        """
        # bypass some checks which sometimes occurs error when saving `Repayment` again
        # for e.g. changing `reconciliation` for already repaid loan will trigger "loan already repaid" error
        if no_checks:
            super(Repayment, self).save()
            return

        if self.loan.repaid_on is not None:
            # we shouldn't register a payment against a loan that's been fully repaid!
            raise LoanAlreadyRepaidError('The loan is already fully repaid. You cannot add more repayments to it.')

        # we can't use the loan outstanding properties because we're in the process of updating
        # the database, so the calculation may be wrong. We use the amount, which is not affected
        # by breakdown() and therefore gives us the correct result
        total_paid = self.loan.repayments.exclude(pk=self.pk).aggregate(paid=Sum('amount'))['paid'] or 0
        total_interest = self.loan.lines.aggregate(amount=Sum('interest'))['amount'] or 0
        total_penalty = self.loan.lines.aggregate(penalty_amount=Sum('penalty'))['penalty_amount'] or 0
        total_subscription = self.loan.lines.aggregate(sub_amount=Sum('subscription'))['sub_amount'] or 0
        max_repayable = self.loan.loan_amount + self.loan.loan_fee + total_interest + total_penalty + total_subscription - total_paid

        if self.amount > max_repayable:
            raise RepaymentTooBigError(
                'The amount repaid exceeds the maximum repayable for this loan',
                params={'max_repayable': max_repayable}
            )

        repayments_to_recalculate = []
        posterior_repayments = self.loan.repayments.filter(date__gt=self.date)
        if posterior_repayments.count() > 0:
            # we already have repayment(s) recorded past the current one:
            # we need to update the breakdown on all posterior repayments
            repayments_to_recalculate = posterior_repayments.order_by('date')

        self.breakdown()

        # save the repayment, subsequent ones and update the loan in a transaction
        # either it's all saved, or nothing
        #
        # careful here: doing recursive transactions is a very bad idea, we need to avoid
        # having more than one transaction started per request
        if update_posterior_repayments:
            with transaction.atomic():
                # save this repayment before we recalc the later ones, so that their
                # breakdown is correct (since breakdown will fetch data from db)
                super(Repayment, self).save(*args, **kwargs)
                for r in repayments_to_recalculate:
                    r.breakdown(force_recalc=True)
                    # prevent crazy recursion when saving by setting the flag
                    r.save(update_posterior_repayments=False)
                self.loan.close_if_fully_repaid(self)
        else:
            # this is only called from *within the transaction*
            # we're in the process of updating repayment breakdown, only save
            # this current repayment, but don't touch any other one.
            super(Repayment, self).save(*args, **kwargs)

            # update (princpal and) interest of loan lines
            # self.loan.update_attributes_for_lines()


class LoanRequestReview(models.Model):
    """
    An review for a loan request. The result can be positive (approved) or
    negative (rejected). The details of the decision are stored in this object.
    If an automatic validation happens, it should create an instance of this class.
    Saving this object will trigger the appropriate state transitions and notifications...
    """
    loan = models.ForeignKey(Loan, related_name='review', on_delete=models.PROTECT)

    timestamp = models.DateTimeField(default=timezone.now)

    # for automated reviews, we'll need to create a django user for the process :)
    reviewer = models.ForeignKey(User, default=1, on_delete=models.PROTECT)

    approved = models.BooleanField(default=False)
    comments = models.TextField(blank=True)

    # if the request is rejected, we can add a link to an alternative loan offer with this field
    # if the request is approved, this field stays empty
    alternative_offer = models.ForeignKey(Loan, default=None, blank=True, null=True, on_delete=models.PROTECT)

    def save(self, *args, **kwargs):
        """
        Save the approval, and trigger the Loan.state transition
        """
        self.loan.approve_request(review=self)
        self.loan.save()

        super(LoanRequestReview, self).save(*args, **kwargs)


class BaseBorrowerSignature(models.Model):
    """
    The base class for all signatures. Built this way, it will require a join on
    every query, but we don't care at this point, it will make coding easier.
    https://docs.djangoproject.com/en/1.10/topics/db/models/#multi-table-inheritance for details.

    For a request to be validated, we could list the Signture types required, each to be valid
    before the request proceeds to next stage.
    """
    loan = models.ForeignKey(Loan, related_name='signatures', on_delete=models.PROTECT)

    # when the app recorded the info
    timestamp = models.DateTimeField(default=timezone.now)
    # when the server got the info
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # result is KO/OK, if KO send to fraud verification
    is_valid = models.NullBooleanField(default=None)

    def validate(self):
        pass

    def save(self, *args, **kwargs):
        """
        Validate the signature and save its status to db
        """
        # self.validate()
        super(BaseBorrowerSignature, self).save(*args, **kwargs)


class PhotoSignature(BaseBorrowerSignature):
    """
    A model to store information about how a borrower signed her loan request.
    """
    photo = StdImageField(
        blank=True,
        upload_to=UploadToUUID(path=settings.LOAN_SIGNATURE_PATH),
        variations=Borrower.borrower_photo_variations,
    )
    # store the raw output from the API call, if it ever becomes useful in the future
    api_result = JSONField(blank=True, null=True)

    def photo_tag(self, photo_obj):
        if photo_obj:
            original_photo_url = photo_obj.url.split('.jpg?')[0]
            url = ''.join([original_photo_url, '.thumbnail.jpg'])
            return mark_safe('<a href="{0}.jpg"><img src="{1}" /></a>'.format(original_photo_url, url))
        else:
            return mark_safe('<p>No photo available</a>')

    def borrower_profile_photo_tag(self):
        """Display the profile photo so we can compare"""
        return self.photo_tag(self.loan.borrower.borrower_photo)

    borrower_profile_photo_tag.short_description = 'Borrower Profile Photo'

    def signature_photo_tag(self):
        return self.photo_tag(self.photo)

    signature_photo_tag.short_description = 'Signature Photo'

    def validate(self):
        """
        Validate the signature by checking that the photo sent is of the borrower
        """
        # self.is_valid = True
        # use AWS Recognition API to compare the faces on the signature photo
        # with the face on the borrower profile photo
        client = boto3.client('rekognition', region_name='eu-west-1')

        # check that we have 2 photos to compare
        if self.photo.name == '':
            raise PhotoSignatureWithoutPhotoError('The signature must include a photo to be valid')
        if self.loan.borrower.borrower_photo.name == '':
            raise BorrowerPhotoMissingError('Please upload a profile photo for {0}'.format(self.loan.borrower.name_en))

        # photos are there, check if they're of the same person
        profile_photo = self.loan.borrower.borrower_photo
        # FIXME: catch HttpError 404 here, if the profile photo doesn't exist
        try:
            with urlopen(profile_photo.thumbnail.url, timeout=10) as f:
                profile_photo_bytes = f.read().strip()
            # if AWS hasn't replied within 10 seconds, we're probably offline anyway, just give up
            with urlopen(self.photo.thumbnail.url, timeout=10) as f:
                signature_photo_bytes = f.read().strip()
        except (HTTPError, URLError) as e:
            # most likely the file was not found because the borrower doesn't have a profile photo
            # the invalid signature will mark the loan as fraudulent so staff can deal with it manually
            self.is_valid = False
            logger = logging.getLogger('root')
            logger.error('PhotoSignature validation error', exc_info=True, extra={
                'signature': self,
                'exception': e,
            })
            return

        try:
            # FIXME: why do we get InvalidParameterException?
            self.api_result = client.compare_faces(
                SourceImage={
                    'Bytes': profile_photo_bytes,
                },
                TargetImage={
                    'Bytes': signature_photo_bytes,
                },
                SimilarityThreshold=50.0
            )
            self.is_valid = (float(self.api_result['FaceMatches'][0]['Similarity']) > 70)
        except Exception as e:
            self.is_valid = False
            logger = logging.getLogger('root')
            logger.error('PhotoSignature face comparison error', exc_info=True, extra={
                'signature': self,
                'exception': e,
            })


DISBURSEMENT_REQUESTED = 'requested'  # default status when creating the disbursement request in the app
DISBURSEMENT_SENT = 'sent'  # money has been sent to the user (we don't know if they withdrew it)
DISBURSEMENT_CANCELLED = 'cancelled'  # for any reason, the loan was rejected, method was changed, etc...
DISBURSEMENT_STATUS_CHOICES = (
    (DISBURSEMENT_REQUESTED, 'Disbursement requested'),
    (DISBURSEMENT_SENT, 'Money disbursed'),
    (DISBURSEMENT_CANCELLED, 'Disbursement cancelled'),
)

DISB_METHOD_WAVE_TRANSFER = 1
DISB_METHOD_BANK_TRANSFER = 2
DISB_METHOD_WAVE_N_CASH_OUT = 3

DISB_METHOD_CHOICES = (
    (DISB_METHOD_WAVE_TRANSFER, 'Wave transfer'),
    (DISB_METHOD_BANK_TRANSFER, 'Bank transfer'),
    (DISB_METHOD_WAVE_N_CASH_OUT, 'Wave Transfer and Cash Out'),
)


class Disbursement(models.Model):
    """
    A model storing information about an actual disbursement.
    Similarly to what happens when a signature object is saved,
    when a Disbursement is saved, it will trigger a loan state change
    and generate notifications as required.
    You should normally not need to create a disbursement using this class,
    use the children proxies instead, that will correctly set data for you.
    """
    # the reference provided by the disbursement provider (Wave...)
    provider_transaction_id = models.CharField(max_length=50, null=True, blank=True)

    timestamp = models.DateTimeField(default=timezone.now)

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # the agent the disbursement was sent to (it should be possible to follow links
    # through loan__borrower_agent to find the agent as well)
    disbursed_to = models.ForeignKey(Agent, on_delete=models.CASCADE)

    # fees paid (to Wave, etc...)
    fees_paid = models.DecimalField(max_digits=12, decimal_places=2)

    # there is a reverse relationship from Loan
    # loans_disbursed (ForeignKey from Loan)
    # because multiple loans can be disbursed in one single operation

    # instead of trying to mess with abstract and child classes, we store specific data
    # in a JSONField so that it can take any shape. We can have proxy child classes
    # that have overloaded methods to perform calculations, checks and so on...

    # so we can use the correct proxy class to handle the data, in case we don't
    # know beforehand
    method = models.IntegerField(choices=DISB_METHOD_CHOICES, default=1)

    # the details specific to each method of transaction. See child proxy classes
    # for explanations of what is found in this for each of them
    details = JSONField()

    state = FSMField(
        default=DISBURSEMENT_REQUESTED,
        choices=DISBURSEMENT_STATUS_CHOICES
    )

    def __str__(self):
        return '{0}: {1} to {2}'.format(DISB_METHOD_CHOICES[self.method - 1][1], self.amount, self.disbursed_to)

    def save(self, *args, **kwargs):
        if self.state == DISBURSEMENT_SENT:
            # FIXME: add validation for checking if Transfer contains correct(same) info. for e.g. method should be the same
            # check for Transfer
            # if not self.transfer:
            #     raise ValidationError('Transfer is missing')

            # check for provider_transaction_id
            # if self.method:
            #     if not self.provider_transaction_id:
            #         raise ValidationError('Transaction id missing')

            # loan state transition
            # FeeNotPaidError will be raised if fee is not paid for some loans
            # normally, the following code will not be run for new disbursement
            # since the default state is DISBURSEMENT_REQUESTED and
            # this code is run only if state is DISBURSEMENT_SENT
            with transaction.atomic():
                for loan in self.loans_disbursed.all():
                    loan.disburse_loan()
                    loan.save()
        super(Disbursement, self).save(*args, **kwargs)


class WaveTransferDisbursement(Disbursement):
    """
    A child class of Disbursement specifically for Wave Transfer.
    """

    class Meta:
        proxy = True

    def __init__(self, *args, **kwargs):
        """
        Set the method attribute by default
        """
        super(WaveTransferDisbursement, self).__init__(*args, **kwargs)
        self.method = DISB_METHOD_WAVE_TRANSFER


class BankTransferDisbursement(Disbursement):
    """
    A child class of Disbursement specifically for Bank Transfer
    """

    class Meta:
        proxy = True

    def __init__(self, *args, **kwargs):
        """
        Set the method attribute by default
        """
        super(BankTransferDisbursement, self).__init__(*args, **kwargs)
        self.method = DISB_METHOD_BANK_TRANSFER


class WaveTransferAndCashOutDisbursement(Disbursement):
    """
    A disbursement in which cash out fees is included
    """

    class Meta:
        proxy = True

    def __init__(self, *args, **kwargs):
        """
        Set the method attribute by default
        """
        super(WaveTransferAndCashOutDisbursement, self).__init__(*args, **kwargs)
        self.method = DISB_METHOD_WAVE_N_CASH_OUT


def default_mfi_branch():
    """
    default MFIBranch created with migration to be called by FieldOfficer
    We set this by default to pick up our initial borrowers, and to avoid
    MFIs seeing each others data in case of error (the new records will be
    assigned to ourselves in case of problem).
    """
    return MFIBranch.objects.get(name='ZigWayMFIBranch').pk


class SuperUsertoLenderPayment(models.Model):
    """
    Payment from SuperUser to Lender
    """
    super_user = models.ForeignKey(Agent, on_delete=models.PROTECT)
    # default_mfi_branch is defined above
    lender = models.ForeignKey(MFIBranch, default=default_mfi_branch, on_delete=models.PROTECT)

    transfer = models.OneToOneField(Transfer, blank=True, null=True, on_delete=models.PROTECT)
    # FIXME: this field maybe redundant since Repayment have FK to this model
    loans = models.ManyToManyField(Loan, blank=True)

    """foreign key to reconciliation table to connect with Repayment(s)"""
    reconciliation = models.ForeignKey(Reconciliation, blank=True, null=True, on_delete=models.PROTECT)

    """reconciliation with Repayment(s)"""
    reconciliation_status = FSMField(
        default=NOT_RECONCILED,
        choices=RECONCILIATION_STATUS_CHOICES
    )

    @transition(
        field=reconciliation_status,
        source=NOT_RECONCILED,
        target=RETURN_VALUE(AUTO_RECONCILED, MANUAL_RECONCILED)
    )
    def set_reconciled(self, intermediary, method):
        self.reconciliation_status = method
        self.reconciliation = intermediary
        self.save()
        return method

    def __str__(self):
        return str(self.super_user.name) + ' ' + str(self.transfer.amount) + ' ' + str(self.transfer.get_method_display()) + ' ' + str(self.transfer.timestamp)


class DefaultPrediction(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='default_prediction')
    passed_credit = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

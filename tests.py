import base64
from datetime import date, datetime, timedelta
import factory
from django.urls import reverse
from freezegun import freeze_time
import json
from PIL import Image
import tempfile
from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from freezegun import freeze_time
from borrowers.models import Borrower
from loans.signals import reconciliation_by_superuser
from payments.models import Transfer, WavePaysbuyPayment
from .models import Currency, Loan, LoanState, RepaymentScheduleLine, Repayment, RepaymentTooBigError, \
    ReasonForDelayedRepayment, NOT_RECONCILED, AUTO_RECONCILED, MANUAL_RECONCILED, NEED_MANUAL_RECONCILIATION, \
    LOAN_DISBURSED, LOAN_REQUEST_SUBMITTED, Disbursement, DISB_METHOD_WAVE_TRANSFER, DISB_METHOD_BANK_TRANSFER, \
    DISB_METHOD_WAVE_N_CASH_OUT, DISBURSEMENT_SENT, LOAN_REQUEST_APPROVED, FeeNotPaidError, DISBURSEMENT_REQUESTED, LOAN_REQUEST_SIGNED, LoanRequestReview, LOAN_REQUEST_REJECTED, SuperUsertoLenderPayment, \
    Reconciliation
from .models import Reconciliation as Recon, ACTUAL_360, ACTUAL_365, EQUAL_REPAYMENTS, MONTHLY, YEARLY
from .serializers import RepaymentSerializer
from .test_factories import BorrowerFactory, CurrencyFactory, LoanFactory, RepaymentFactory, AgentFactory, UserFactory, DisbursementFactory
from sms_gateway.models import SMSMessage, WaveMoneyReceiveSMS
from .tasks import repayments_by_sender, reconciliation
from django.utils import timezone
import pytz
from django.contrib.auth.models import Permission, User
import phonenumbers as pn
from django.core.exceptions import ValidationError
from payments.test_factories import TransferFactory, SuperUsertoLenderPaymentFactory
from string import Template
from unittest.mock import MagicMock
import requests
from django_fsm import TransitionNotAllowed
from faker import Faker


class LoanContractingTests(TestCase):
    """
    Test creation of Loan objects and saving them. No repayment handling in here.
    """

    def test_outstanding_amounts_correct_after_creating_loan(self):
        l = LoanFactory(
            loan_amount=40000,
            loan_fee=1250,
            normal_repayment_amount=2000,
            bullet_repayment_amount=8000,
        )
        self.assertEqual(l.fee_outstanding, 1250)
        self.assertEqual(l.principal_outstanding, 40000)

    def test_repayment_breakdown_with_amount_due(self):
        """
        Verify that outstanding amounts and repayment breakdown are ok. Twice.
        """
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=60000,
                normal_repayment_amount=2500,
                bullet_repayment_amount=2500,
                loan_fee=1200
            )
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=3700
        )
        r.save()
        # verify that breakdown is correct
        self.assertEqual(r.principal, 2500)
        self.assertEqual(r.fee, 1200)
        self.assertEqual(r.interest, 0)
        self.assertEqual(r.penalty, 0)

        # verify that the loan outstanding amounts were correctly updated
        self.assertEqual(loan.principal_outstanding, 60000 - 2500)
        self.assertEqual(loan.fee_outstanding, 0)

        # record a second repayment
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 29),
            amount=2500
        )
        r.save()
        # verify that breakdown is correct
        self.assertEqual(r.principal, 2500)
        self.assertEqual(r.fee, 0)
        self.assertEqual(r.interest, 0)
        self.assertEqual(r.penalty, 0)

        # verify that the loan outstanding amounts were correctly updated
        self.assertEqual(loan.principal_outstanding, 60000 - (2500 * 2))
        self.assertEqual(loan.fee_outstanding, 0)

    def test_outstanding_amount_for_date_handles_late_payment(self):
        """
        Miss first payment, check the outstanding for day 2 is correct
        """
        with freeze_time(date(2016, 10, 30)):
            loan = LoanFactory(
                loan_amount=50000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=10000,
                loan_fee=400
            )
        with freeze_time('2016-10-31'):
            o = loan.amount_due_for_date(date.today())
            self.assertEqual(o['principal'], 10000)
            self.assertEqual(o['fee'], 400)
        with freeze_time('2016-11-01'):
            o = loan.amount_due_for_date(date.today())
            self.assertEqual(o['principal'], 20000)
            self.assertEqual(o['fee'], 400)
        # record a payment of just the right amount
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 31),
            amount=10400
        )
        r.save()
        with freeze_time('2016-11-01'):
            o = loan.amount_due_for_date(date.today())
            self.assertEqual(o['principal'], 10000)
            self.assertEqual(o['fee'], 0)

    def test_early_partial_repayment_updates_outstanding_amounts(self):
        """
        Borrower pays too much on first day, test outstanding_amount is ok
        """
        with freeze_time(date(2016, 10, 30)):
            loan = LoanFactory(
                loan_amount=50000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=10000,
                loan_fee=400
            )
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 31),
            amount=20400
        )
        r.save()
        with freeze_time('2016-10-31'):
            o = loan.amount_due_for_date(date.today())
            self.assertEqual(o['principal'], 0)
            self.assertEqual(o['fee'], 0)
        with freeze_time('2016-11-01'):
            o = loan.amount_due_for_date(date.today())
            self.assertEqual(o['principal'], 0)
            self.assertEqual(o['fee'], 0)
        with freeze_time('2016-11-02'):
            o = loan.amount_due_for_date(date.today())
            self.assertEqual(o['principal'], 10000)
            self.assertEqual(o['fee'], 0)

    def test_repayment_breakdown_with_insufficient_repayment(self):
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=60000,
                normal_repayment_amount=2500,
                bullet_repayment_amount=2500,
                loan_fee=1200
            )
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=3400
        )
        r.save()
        # verify that breakdown is correct
        self.assertEqual(r.principal, 2200)
        self.assertEqual(r.fee, 1200)
        self.assertEqual(r.interest, 0)
        self.assertEqual(r.penalty, 0)

        # verify that the loan outstanding amounts were correctly updated
        self.assertEqual(loan.principal_outstanding, 60000 - 2200)
        self.assertEqual(loan.fee_outstanding, 0)

    def test_repayment_recording_in_non_chronological_order(self):
        """
        Try to save a payment that comes before later ones
        Breakdown and outstanding amounts should be corrected automatically
        as repayments are saved.
        """
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=2000,
                bullet_repayment_amount=2000,
                loan_fee=1200
            )
        r1 = Repayment(
            loan=loan,
            date=date(2016, 10, 29),
            amount=2000
        )
        r1.save()  # fee 1200, princ 800
        # the refresh_from_db() calls are to make sure we're running asserts()
        # on the content of the database, which is changed when save()ing repayments
        r1.refresh_from_db()

        r2 = Repayment(
            loan=loan,
            date=date(2016, 10, 30),
            amount=2000
        )
        r2.save()  # fee 0, princ 2000
        r2.refresh_from_db()

        r3 = Repayment(
            loan=loan,
            date=date(2016, 10, 31),
            amount=2000
        )
        r3.save()  # fee 0, princ 2000
        self.assertEqual(r1.principal, 800)
        self.assertEqual(r1.fee, 1200)
        self.assertEqual(r2.fee, 0)

        # now insert an earlier repayment that we forgot to save
        r4 = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=2000
        )
        r4.save()  # fee 1200, princ 800, and all others get fee 0, princ 2000
        r4.refresh_from_db()

        self.assertEqual(r4.principal, 800)
        self.assertEqual(r4.fee, 1200)
        self.assertEqual(r2.fee, 0)
        r1.refresh_from_db()
        r3.refresh_from_db()
        self.assertEqual(r1.fee, 0)
        self.assertEqual(r3.fee, 0)
        self.assertEqual(r3.principal, 2000)

        loan.refresh_from_db()
        self.assertEqual(loan.principal_outstanding, 10000 - 3 * 2000 - 800)
        self.assertEqual(loan.fee_outstanding, 0)

    def test_close_loan_with_non_ordered_repayment(self):
        """
        Verify the `repaid_on` date is set correctly even if the loan is closed
        by another repayment than the last one. (this emulates manual unordered
        recording of repayments.
        """
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=5000,
                bullet_repayment_amount=0,
                state=LOAN_DISBURSED,
                loan_fee=200
            )
        r1 = Repayment(
            loan=loan,
            date=date(2016, 10, 29),
            amount=5000
        )
        r1.save()
        r2 = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=5200
        )
        r2.save()  # this should close the loan
        # verify that the loan is closed by the last repayment
        # not the one saved last
        loan.refresh_from_db()
        self.assertEqual(loan.repaid_on, date(2016, 10, 29))

    def test_repayment_exceeding_loan_outstanding_amount(self):
        """
        Try to save a payment that is higher than the outstanding loan amount,
        it should raise an error.
        """
        loan_amount = 10000
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=loan_amount,
                normal_repayment_amount=2500,
                bullet_repayment_amount=2500,
                loan_fee=1200
            )
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=20000
        )
        with self.assertRaises(RepaymentTooBigError):
            r.save()

    def test_repayment_breakdown_with_early_and_closing_repayments(self):
        """
        Repay the loan early, once not entirely then fully, verify that breakdown is ok, and
        that the loan is closed properly.
        """
        loan_amount = 20000
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=loan_amount,
                normal_repayment_amount=2500,
                bullet_repayment_amount=2500,
                state=LOAN_DISBURSED,
                loan_fee=1200
            )
        # first repayment is as expected
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=3700
        )
        r.save()
        # verify that breakdown is correct
        self.assertEqual(r.principal, 2500)
        self.assertEqual(r.fee, 1200)

        # verify that the loan outstanding amounts were correctly updated
        self.assertEqual(loan.principal_outstanding, loan_amount - 2500)
        self.assertEqual(loan.fee_outstanding, 0)
        self.assertEqual(loan.repaid_on, None)

        # repay part of the loan early
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 29),
            amount=7500
        )
        with freeze_time('2016-10-29'):
            r.save()
            self.assertEqual(r.principal, 7500)
            self.assertEqual(r.fee, 0)

            self.assertEqual(loan.principal_outstanding, loan_amount - 10000)
            self.assertEqual(loan.fee_outstanding, 0)
            self.assertEqual(loan.repaid_on, None)

        # repay the rest of the loan
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 31),
            amount=10000
        )
        with freeze_time('2016-10-31'):
            r.save()
            self.assertEqual(r.principal, 10000)
            self.assertEqual(r.fee, 0)

            self.assertEqual(loan.principal_outstanding, 0)
            self.assertEqual(loan.fee_outstanding, 0)
            self.assertEqual(loan.repaid_on, date(2016, 10, 31))

    def test_delay_with_no_delay(self):
        """
        delay must be 0 on a loan repaid as planned
        """
        initial_date = date(2016, 10, 27)
        with freeze_time(initial_date):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=1000,
                bullet_repayment_amount=1000,
                state=LOAN_DISBURSED,
                loan_fee=200
            )
        for d in range(0, 10):
            r = Repayment(
                loan=loan,
                date=initial_date + timedelta(days=d + 1),
                amount=1000 + 200 * (d == 0)
            )
            r.save()
        with freeze_time('2016-12-01'):
            self.assertEqual(loan.repaid_on, initial_date + timedelta(days=10))
            self.assertEqual(loan.get_delay(), 0)
            self.assertEqual(loan.current_delay, 0)

    def test_delay_with_no_payments(self):
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=1000,
                bullet_repayment_amount=1000,
                loan_fee=200
            )
        with freeze_time('2016-10-29'):
            self.assertEqual(loan.get_delay(), 1)
            self.assertEqual(loan.current_delay, 1)
        with freeze_time('2016-10-31'):
            self.assertEqual(loan.get_delay(), 3)
            self.assertEqual(loan.current_delay, 3)

    def test_delay_with_early_and_late_payments(self):
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=1000,
                bullet_repayment_amount=1000,
                loan_fee=200
            )
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 28),
            amount=2200
        )
        r.save()
        with freeze_time('2016-10-29'):
            self.assertEqual(loan.get_delay(), 0)
            self.assertEqual(loan.current_delay, 0)
        with freeze_time('2016-10-31'):
            self.assertEqual(loan.get_delay(), 1)
            self.assertEqual(loan.current_delay, 1)

        # get a late payment to cover the missed ones
        r = Repayment(
            loan=loan,
            date=date(2016, 11, 1),
            amount=3000
        )
        r.save()
        # a day of delay should not be cancelled by a late repayment
        # here, get_delay and current_delay behave differently.
        with freeze_time('2016-11-01'):
            self.assertEqual(loan.get_delay(), 2)
            self.assertEqual(loan.current_delay, 0)
        with freeze_time('2016-11-02'):
            self.assertEqual(loan.get_delay(), 2)
            self.assertEqual(loan.current_delay, 0)

    def test_current_delay_with_not_enough_repayment(self):
        """
        Repayment is received but not enough to cover
        missing repayments
        """
        with freeze_time(date(2018, 1, 1)):
            loan = LoanFactory(
                loan_amount=40000,
                normal_repayment_amount=5000,
                bullet_repayment_amount=5000,
                loan_fee=200
            )

        with freeze_time('2018-1-4'):
            self.assertEqual(loan.current_delay, 2)
            r = Repayment(
                loan=loan,
                date=date(2018, 1, 4),
                amount=5200
            )
            r.save()
            # repayment is only enough for one day
            # so, current_delay should not be changed
            self.assertEqual(loan.current_delay, 2)

    def test_current_delay_at(self):
        """
        current_delay_at should output differently
        according to when the function is called
        """
        with freeze_time(date(2018, 1, 10)):
            loan = LoanFactory(
                loan_amount=35000,
                normal_repayment_amount=2000,
                bullet_repayment_amount=5000,
                loan_fee=200
            )

        with freeze_time('2018-1-12'):
            self.assertEqual(loan.current_delay_at(date(2018, 1, 12)), 1)
            r = Repayment(
                loan=loan,
                date=date(2018, 1, 12),
                amount=2200
            )
            r.save()
            # repayment is enough for previous day
            # so, current_delay should be changed
            # since this day is ongoing, there is still hope that the repayment for this day is coming
            # so, whether this day is delay or not is cannot be decided at this point
            self.assertEqual(loan.current_delay_at(date(2018, 1, 12)), 0)

        with freeze_time('2018-1-13'):
            # here, we are calling current_delay_at for previous day
            # since that day is already passed, it is sure that no new repayments are coming for that day
            # so, whether this day is delay or not is can be decided at this point
            self.assertEqual(loan.current_delay_at(date(2018, 1, 12)), 1)

    def test_current_delay_at_2(self):
        """
        check if
        1. delay for previous day and today should be the same
        if no repayments between them
        2. it should work properly for days with no planned repayments.
        that happen after last repayment date
        """
        with freeze_time(date(2016, 10, 27)):
            loan = LoanFactory(
                loan_amount=9000,
                normal_repayment_amount=3000,
                bullet_repayment_amount=3000,
                loan_fee=200
            )
        with freeze_time('2016-10-28'):
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), loan.current_delay_at(date.today()))
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), 0)
            self.assertEqual(loan.current_delay_at(date.today()), 0)
        with freeze_time('2016-10-29'):
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), loan.current_delay_at(date.today()))
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), 1)
            self.assertEqual(loan.current_delay_at(date.today()), 1)
        with freeze_time('2016-10-30'):
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), loan.current_delay_at(date.today()))
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), 2)
            self.assertEqual(loan.current_delay_at(date.today()), 2)
        # no planned repayments starting from this day
        with freeze_time('2016-10-31'):
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), loan.current_delay_at(date.today()))
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), 3)
            self.assertEqual(loan.current_delay_at(date.today()), 3)
        with freeze_time('2016-11-1'):
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), loan.current_delay_at(date.today()))
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), 4)
            self.assertEqual(loan.current_delay_at(date.today()), 4)
        with freeze_time('2016-11-2'):
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), loan.current_delay_at(date.today()))
            self.assertEqual(loan.current_delay_at(date.today() - timedelta(days=1)), 5)
            self.assertEqual(loan.current_delay_at(date.today()), 5)

    def test_next_disbursement_date_with_no_repayment(self):
        with freeze_time(date(2016, 10, 31)):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=1000,
                bullet_repayment_amount=1000,
                state=LOAN_DISBURSED,
                loan_fee=200
            )
        with freeze_time('2016-11-05'):
            self.assertEqual(loan.get_delay(), 4)
            self.assertEqual(loan.current_delay, 4)
            self.assertEqual(loan.next_disbursement_date, date(2016, 11, 15))
        with freeze_time('2016-11-11'):
            self.assertEqual(loan.get_delay(), 10)
            self.assertEqual(loan.current_delay, 10)
            self.assertEqual(loan.next_disbursement_date, date(2016, 11, 21))
        # now repay the full loan in one go
        r = Repayment(
            loan=loan,
            date=date(2016, 11, 10),
            amount=10200
        )
        r.save()
        # here get_delay and current_delay behave differently
        with freeze_time('2016-11-11'):
            self.assertEqual(loan.get_delay(), 9)
            self.assertEqual(loan.current_delay, 0)
            self.assertEqual(loan.next_disbursement_date, date(2016, 11, 20))

    def test_total_amount_due_for_date(self):
        """
        testing total_amount_due_for_date
        """
        with freeze_time(date(2016, 10, 30)):
            loan = LoanFactory(
                loan_amount=50000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=10000,
                loan_fee=400
            )
        # very first day (no repayment needed)
        with freeze_time('2016-10-30'):
            self.assertEqual(loan.total_amount_due_for_date(date.today()), 0)
        # second day - before repayment
        with freeze_time('2016-10-31'):
            self.assertEqual(loan.total_amount_due_for_date(date.today()), 10400)
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 31),
            amount=10400
        )
        r.save()
        # second day - after repayment
        with freeze_time('2016-10-31'):
            self.assertEqual(loan.total_amount_due_for_date(date.today()), 0)
        # third and fourth day - no repayment is received
        with freeze_time('2016-11-01'):
            self.assertEqual(loan.total_amount_due_for_date(date.today()), 10000)
        with freeze_time('2016-11-02'):
            self.assertEqual(loan.total_amount_due_for_date(date.today()), 20000)
        # repayment received on fifth day
        r = Repayment(
            loan=loan,
            date=date(2016, 11, 3),
            amount=5000
        )
        r.save()
        with freeze_time('2016-11-03'):
            self.assertEqual(loan.total_amount_due_for_date(date.today()), 25000)
        # check if total_amount_due_for_date for previous days stay the same
        self.assertEqual(loan.total_amount_due_for_date(date(2016, 10, 30)), 0)
        self.assertEqual(loan.total_amount_due_for_date(date(2016, 10, 31)), 0)
        self.assertEqual(loan.total_amount_due_for_date(date(2016, 11, 1)), 10000)
        self.assertEqual(loan.total_amount_due_for_date(date(2016, 11, 2)), 20000)
        self.assertEqual(loan.total_amount_due_for_date(date(2016, 11, 3)), 25000)

    def test_self_loan_amount_fix(self):
        """
        check if self.loan_amount is not changed by repayments
        """
        with freeze_time(date(2016, 10, 30)):
            loan = LoanFactory(
                loan_amount=50000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=10000,
                loan_fee=400
            )
        self.assertEqual(loan.loan_amount, 50000)
        r = Repayment(
            loan=loan,
            date=date(2016, 10, 31),
            amount=10400
        )
        r.save()
        self.assertEqual(loan.loan_amount, 50000)

    def test_double_early_repayments(self):
        """
        Repay the loan early with two repayments and check if the loan is closed.
        """
        with freeze_time('2017-6-10'):
            ln1 = LoanFactory.create(
                loan_amount=25000,
                normal_repayment_amount=1000,
                state=LOAN_DISBURSED,
                loan_fee=0,
            )
        with freeze_time('2017-6-11'):
            Repayment.objects.create(
                loan=ln1,
                date=date.today(),
                amount=10000,
            )
        with freeze_time('2017-6-12'):
            Repayment.objects.create(
                loan=ln1,
                date=date.today(),
                amount=15000,
            )
            self.assertEqual(ln1.repaid_on, date(2017, 6, 12))

    def test_multiple_early_repayments(self):
        """
        test case with multple early repyaments
        """
        with freeze_time('2016-10-27'):
            ln = LoanFactory.create(
                contract_date=date(2016, 10, 27),
                loan_amount=20000,
                normal_repayment_amount=2500,
                bullet_repayment_amount=2500,
                loan_fee=1200,
            )
        with freeze_time('2016-10-28'):
            # paid the triple amount of normal repayment
            r = Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=2500 * 3 + 1200
            )
            self.assertEqual(r.principal, 7500)
            self.assertEqual(r.fee, 1200)
            self.assertEqual(ln.principal_outstanding, 20000 - 7500)
        with freeze_time('2016-10-29'):
            # paid the double amount of normal repayment
            r = Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=2500 * 2
            )
        self.assertEqual(r.principal, 5000)
        self.assertEqual(r.fee, 0)
        self.assertEqual(ln.principal_outstanding, 20000 - 12500)


class LoanInterestTests():
    """
    Test cases for interest calculation
    """

    def test_calculate_interest(self):
        # loan_interest_type ACTUAL_360
        with freeze_time(date(2017, 1, 30)):
            loan1 = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=1000,
                bullet_repayment_amount=1000,
                loan_interest_type=ACTUAL_360,
                loan_interest_rate=2.5,
                loan_fee=0
            )
        # the magic numbers are by hand calculation according to formulae
        self.assertEqual(loan1.calculate_interest(loan1.loan_amount), 8)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 25000, 2.5, MONTHLY, 10), 208)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 40000, 2.0, MONTHLY, 3), 80)

        # loan_interest_type ACTUAL_365
        with freeze_time(date(2017, 2, 5)):
            loan2 = LoanFactory(
                loan_amount=30000,
                normal_repayment_amount=2000,
                bullet_repayment_amount=5000,
                loan_interest_type=ACTUAL_365,
                loan_interest_rate=2.5,
                loan_fee=0
            )
        # the magic numbers are by hand calculation according to formulae
        self.assertEqual(loan2.calculate_interest(loan2.loan_amount), 25)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 34000, 2.5, MONTHLY, 9), 252)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 100000, 1.9, MONTHLY, 5), 312)

        # loan_interest_type EQUAL_REPAYMENTS
        with freeze_time(date(2016, 10, 30)):
            loan3 = LoanFactory(
                loan_amount=60000,
                number_of_repayments=3,
                normal_repayment_amount=Loan.calculate_equal_repayment_amount_for_loan_initialization(60000, 2.5 * 10, 3),
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=2.5 * 10,
                loan_fee=0
            )
        # the magic numbers are by hand calculation according to formulae
        self.assertEqual(loan3.calculate_interest(loan3.loan_amount, 1, loan3.loan_amount), 493)
        self.assertEqual(loan3.calculate_interest(40163, 2, loan3.loan_amount), 331)
        self.assertEqual(loan3.calculate_interest(20164, 3, loan3.loan_amount), 166)

        # loan_interest_type EQUAL_REPAYMENTS with early and late
        # FIXME: this test uses actual system time, which is not reproducible
        loan4 = LoanFactory(
            loan_amount=20000,
            number_of_repayments=5,
            normal_repayment_amount=Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5 * 7, 5),
            bullet_repayment_amount=0,
            loan_interest_type=EQUAL_REPAYMENTS,
            loan_interest_rate=2.5 * 7,
            loan_fee=0
        )
        # the magic numbers are by hand calculation according to formulae
        self.assertEqual(loan4.calculate_interest(loan4.loan_amount, 1, loan4.loan_amount), 115)
        self.assertEqual(loan4.calculate_interest(4046, 5, loan4.loan_amount), 23)
        # early
        self.assertEqual(loan4.calculate_interest(6560, 4, 13046, 1), 38)
        # late
        self.assertEqual(loan4.calculate_interest(5801, 5, 11569, 3), 33)

    def test_calculate_interest_2(self):
        # the magic numbers are done by hand calculation

        # ACTUAL_360 MONTHLY
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 60000, 2.5, MONTHLY, 10), 500)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 40000, 2.5, MONTHLY, 10), 333)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 20000, 2.5, MONTHLY, 5), 83)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 10000, 2.5, MONTHLY, 5), 42)

        # ACTUAL_360 YEARLY
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 20000, 30, YEARLY, 7), 117)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 16000, 30, YEARLY, 7), 93)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 12000, 30, YEARLY, 7), 70)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 8000, 30, YEARLY, 7), 47)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_360, 4000, 30, YEARLY, 7), 23)

        # ACTUAL_365
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 60000, 2.5, MONTHLY, 10), 493)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 40000, 2.5, MONTHLY, 10), 329)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 20000, 2.5, MONTHLY, 5), 82)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 10000, 2.5, MONTHLY, 5), 41)

        # ACTUAL_365 YEARLY
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 20000, 30, YEARLY, 7), 115)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 16000, 30, YEARLY, 7), 92)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 12000, 30, YEARLY, 7), 69)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 8000, 30, YEARLY, 7), 46)
        self.assertEqual(Loan._calculate_interest_for_actual_360_or_365(ACTUAL_365, 4000, 30, YEARLY, 7), 23)

    def test_interest_with_perfect_repayments(self):
        """
        borrower repay according to schedule and correct amount
        """
        # test case with repayments received
        with freeze_time('2017-2-10'):
            ln = LoanFactory(
                loan_amount=50000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=20000,
                loan_interest_rate=2.5,
                loan_fee=400
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # check if every line have correct interest
            lines = ln.lines.all()
            self.assertEqual(lines.get(date=date(2017, 2, 11)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 2, 12)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2017, 2, 13)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 2, 14)).interest, ln.calculate_interest(20000))
        with freeze_time('2017-2-11'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(50000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 2, 11),
                amount=10000 + 400 + ln.calculate_interest(50000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # interest of every future lines should be not changed since Repayment is correct
            self.assertEqual(lines.get(date=date(2017, 2, 11)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 2, 12)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2017, 2, 13)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 2, 14)).interest, ln.calculate_interest(20000))
        with freeze_time('2017-2-12'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(40000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 2, 12),
                amount=10000 + ln.calculate_interest(40000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # interest of every future lines should not be changed since Repayment is correct
            self.assertEqual(lines.get(date=date(2017, 2, 11)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 2, 12)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2017, 2, 13)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 2, 14)).interest, ln.calculate_interest(20000))
        with freeze_time('2017-2-13'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(30000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 2, 13),
                amount=10000 + ln.calculate_interest(30000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # interest of every future lines should be not changed since Repayment is correct
            self.assertEqual(lines.get(date=date(2017, 2, 11)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 2, 12)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2017, 2, 13)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 2, 14)).interest, ln.calculate_interest(20000))
        with freeze_time('2017-2-14'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(20000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 2, 14),
                amount=20000 + ln.calculate_interest(20000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # since this is last repayment, nothing should be changed
            self.assertEqual(lines.get(date=date(2017, 2, 11)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 2, 12)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2017, 2, 13)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 2, 14)).interest, ln.calculate_interest(20000))
            # loan should be closed
            self.assertEqual(ln.repaid_on, date(2017, 2, 14))

    def test_interest_early_and_late_repayments(self):
        """
        borrower repay early one time and late one time
        """
        # test case with early repayments and late repayments
        with freeze_time('2015-11-12'):
            ln = LoanFactory(
                loan_amount=70000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=20000,
                loan_interest_rate=2.5,
                loan_fee=600
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # each line have should have respective interest
            lines = ln.lines.all()
            self.assertEqual(lines.get(date=date(2015, 11, 13)).interest, ln.calculate_interest(70000))
            self.assertEqual(lines.get(date=date(2015, 11, 14)).interest, ln.calculate_interest(60000))
            self.assertEqual(lines.get(date=date(2015, 11, 15)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 16)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 17)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2015, 11, 18)).interest, ln.calculate_interest(20000))
        with freeze_time('2015-11-13'):
            # update interest
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(70000))
            # notice amount is twice of normal repayment. This will be early repayment
            Repayment.objects.create(
                loan=ln,
                date=date(2015, 11, 13),
                amount=20000 + 600 + ln.calculate_interest(70000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # interest of some of future lines should be changed since Repayment is received
            self.assertEqual(lines.get(date=date(2015, 11, 13)).interest, ln.calculate_interest(70000))
            self.assertEqual(lines.get(date=date(2015, 11, 14)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 15)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 16)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 17)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2015, 11, 18)).interest, ln.calculate_interest(20000))
        # notice one day is skipped and borrower already repaid for this day via early repayment
        with freeze_time('2015-11-15'):
            # update interest
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(50000) * 2)
            # notice interest is for 2 days
            Repayment.objects.create(
                loan=ln,
                date=date(2015, 11, 15),
                amount=10000 + ln.calculate_interest(50000) * 2
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # interest of future lines should not be changed since Repayment is correct
            self.assertEqual(lines.get(date=date(2015, 11, 13)).interest, ln.calculate_interest(70000))
            self.assertEqual(lines.get(date=date(2015, 11, 14)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 15)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 16)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 17)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2015, 11, 18)).interest, ln.calculate_interest(20000))
        with freeze_time('2015-11-17'):
            # update interest
            ln.update_attributes_for_lines()
            # interest of 2015-11-17 is calculate on 40000 since borrower missed repayment yesterday
            self.assertEqual(lines.get(date=date(2015, 11, 13)).interest, ln.calculate_interest(70000))
            self.assertEqual(lines.get(date=date(2015, 11, 14)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 15)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 16)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 17)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 18)).interest, ln.calculate_interest(20000))
            # update interest
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(40000) * 2)
            # notice one day is skipped again. But the repayment is twice of normal amount. So, this is late repayment
            # again notice interest is for 2 days
            Repayment.objects.create(
                loan=ln,
                date=date(2015, 11, 17),
                amount=20000 + ln.calculate_interest(40000) * 2
            )
            self.assertEqual(ln.interest_outstanding, 0)
        with freeze_time('2015-11-18'):
            # update interest
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(20000))
            # this is bullet repayment
            Repayment.objects.create(
                loan=ln,
                date=date(2015, 11, 18),
                amount=20000 + ln.calculate_interest(20000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # since this is the last repayment nothing should be changed
            self.assertEqual(lines.get(date=date(2015, 11, 13)).interest, ln.calculate_interest(70000))
            self.assertEqual(lines.get(date=date(2015, 11, 14)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 15)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2015, 11, 16)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 17)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2015, 11, 18)).interest, ln.calculate_interest(20000))
            # loan should be closed
            self.assertEqual(ln.repaid_on, date(2015, 11, 18))

    def test_interest_repay_early_in_one_shot(self):
        """
        repay all loan early
        """
        with freeze_time('2017-1-5'):
            ln = LoanFactory(
                loan_amount=50000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=20000,
                loan_interest_rate=2.5,
                loan_fee=400
            )
            # each line should have respective interest for now
            lines = ln.lines.all()
            self.assertEqual(lines.get(date=date(2017, 1, 6)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 1, 7)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2017, 1, 8)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 1, 9)).interest, ln.calculate_interest(20000))
        with freeze_time('2017-1-6'):
            # check if interest outstanding is correct
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(50000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 1, 6),
                amount=50000 + 400 + ln.calculate_interest(50000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
            # interest of every future lines should be zero since loan is paid
            self.assertEqual(lines.get(date=date(2017, 1, 6)).interest, ln.calculate_interest(50000))
            self.assertEqual(lines.get(date=date(2017, 1, 7)).interest, ln.calculate_interest(0))
            self.assertEqual(lines.get(date=date(2017, 1, 8)).interest, ln.calculate_interest(0))
            self.assertEqual(lines.get(date=date(2017, 1, 9)).interest, ln.calculate_interest(0))
            # loan should be closed
            self.assertEqual(ln.repaid_on, date(2017, 1, 6))

    def test_interesting_outstanding(self):
        """
        beware that decimal and float give different values sometimes
        every calculation are done in decimal
        """
        # borrower repay everyday perfectly
        # before repayment there is interest outstanding, but after correct repayment outstanding is zero.
        with freeze_time('2017-3-08'):
            ln = LoanFactory(
                loan_amount=25000,
                normal_repayment_amount=5000,
                bullet_repayment_amount=10000,
                loan_interest_rate=2.5,
                loan_fee=400
            )
            # the day loan is received. So, no interest
            self.assertEqual(ln.interest_outstanding, 0)
        with freeze_time('2017-3-09'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(25000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 3, 9),
                amount=5000 + 400 + ln.calculate_interest(25000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
        with freeze_time('2017-3-10'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(20000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 3, 10),
                amount=5000 + ln.calculate_interest(20000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
        with freeze_time('2017-3-11'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(15000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 3, 11),
                amount=5000 + ln.calculate_interest(15000)
            )
            self.assertEqual(ln.interest_outstanding, 0)
        with freeze_time('2017-3-12'):
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(10000))
            Repayment.objects.create(
                loan=ln,
                date=date(2017, 3, 12),
                amount=10000 + ln.calculate_interest(10000)
            )
            self.assertEqual(ln.interest_outstanding, 0)

    def test_interesting_outstanding_not_normal_repayment(self):
        """
        borrower pay partial or more than normal but not multiple of normal amount
        """
        with freeze_time('2016-11-2'):
            ln = LoanFactory(
                loan_amount=40000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=10000,
                loan_interest_type=ACTUAL_360,
                loan_interest_rate=2.5,
                loan_fee=400
            )
        # partial repayment
        with freeze_time('2016-11-3'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(40000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=4000 + 400 + ln.calculate_interest(40000)
            )
        # repay missing amount from yesterday and normal amount from today
        with freeze_time('2016-11-4'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(40000 - 4000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=16000 + ln.calculate_interest(40000 - 4000)
            )
        # repay normal+partial
        with freeze_time('2016-11-5'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(40000 - 20000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=18000 + ln.calculate_interest(40000 - 20000)
            )
        with freeze_time('2016-11-6'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(40000 - 38000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=2000 + ln.calculate_interest(40000 - 38000)
            )
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2016, 11, 6))

    def test__calculate_components_for_equal_repayments(self):
        """
        test private function _calculate_components_for_equal_repayments
        this function is only used for loan_interest_type EQUAL_REPAYMENTS
        magic numbers are done by hand calculation
        test cases are from Miranda excel
        """
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=20000, balance=20000, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=1),
            {'repayment': 4069, 'principal': 3954, 'interest': 115, 'balance': 16046})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=20000, balance=16046, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=2),
            {'repayment': 4069, 'principal': 3977, 'interest': 92, 'balance': 12069})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=20000, balance=12069, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=3),
            {'repayment': 4069, 'principal': 4000, 'interest': 69, 'balance': 8069})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=20000, balance=8069, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=4),
            {'repayment': 4069, 'principal': 4023, 'interest': 46, 'balance': 4046})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=20000, balance=4046, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=5),
            {'repayment': 4069, 'principal': 4046, 'interest': 23, 'balance': 0})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=13046, balance=13046, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=2, day_of_restart=1),
            {'repayment': 3309, 'principal': 3233, 'interest': 75, 'balance': 9813})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=13046, balance=9812, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=3, day_of_restart=1),
            {'repayment': 3309, 'principal': 3252, 'interest': 57, 'balance': 6560})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=13046, balance=6560, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=4, day_of_restart=1),
            {'repayment': 3309, 'principal': 3270, 'interest': 38, 'balance': 3290})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=13046, balance=3290, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=5, day_of_restart=1),
            {'repayment': 3309, 'principal': 3290, 'interest': 19, 'balance': 0})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=5069, balance=5069, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=4, day_of_restart=3),
            {'repayment': 2556, 'principal': 2527, 'interest': 29, 'balance': 2542})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=5069, balance=2542, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=5, day_of_restart=3),
            {'repayment': 2556, 'principal': 2542, 'interest': 14, 'balance': 0})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=19546, balance=19546, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=2, day_of_restart=1),
            {'repayment': 4957, 'principal': 4845, 'interest': 112, 'balance': 14701})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=19546, balance=14701, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=3, day_of_restart=1),
            {'repayment': 4957, 'principal': 4872, 'interest': 85, 'balance': 9829})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=19546, balance=9829, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=4, day_of_restart=1),
            {'repayment': 4957, 'principal': 4900, 'interest': 57, 'balance': 4929})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=19546, balance=4929, interest_rate=30, interest_duration=YEARLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=5, day_of_restart=1),
            {'repayment': 4957, 'principal': 4929, 'interest': 28, 'balance': 0})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=11569, balance=11569, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=4, day_of_restart=3),
            {'repayment': 5834, 'principal': 5768, 'interest': 67, 'balance': 5801})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=11569, balance=5801, interest_rate=2.5, interest_duration=MONTHLY, interest_period=7,
                number_of_repayments=5, number_of_periods_since_contract=5, day_of_restart=3),
            {'repayment': 5834, 'principal': 5801, 'interest': 33, 'balance': 0})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=60000, balance=60000, interest_rate=2.5, interest_duration=MONTHLY, interest_period=10,
                number_of_repayments=3, number_of_periods_since_contract=1),
            {'repayment': 20330, 'principal': 19837, 'interest': 493, 'balance': 40163})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=60000, balance=40163, interest_rate=2.5, interest_duration=MONTHLY, interest_period=10,
                number_of_repayments=3, number_of_periods_since_contract=2),
            {'repayment': 20330, 'principal': 19999, 'interest': 331, 'balance': 20164})
        self.assertDictEqual(
            Loan._calculate_components_for_equal_repayments(
                base_balance=60000, balance=20164, interest_rate=2.5, interest_duration=MONTHLY, interest_period=10,
                number_of_repayments=3, number_of_periods_since_contract=3),
            {'repayment': 20330, 'principal': 20164, 'interest': 166, 'balance': 0})

    def test_calculate_normal_repayment_amount_for_equal_repayments(self):
        """
        Magic numbers are from Miranda excel
        """
        self.assertEqual(
            Loan.calculate_equal_repayment_amount_for_loan_initialization(60000, 2.5, 3, interest_period=10), 20330)
        self.assertEqual(
            Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 30, 5, interest_duration=YEARLY,
                                                                          interest_period=7), 4069)
        self.assertEqual(
            Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5, 5, interest_period=7), 4069)

    def test_update_attributes_for_today_and_future_lines_normal(self):
        """
        perfect case where borrower pay daily and correctly
        """
        with freeze_time('2017-3-1'):
            ln = LoanFactory(
                loan_amount=30000,
                normal_repayment_amount=10000,
                bullet_repayment_amount=10000,
                loan_interest_rate=2.5,
                loan_fee=0
            )
            lines = ln.lines.all()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
        with freeze_time('2017-3-2'):
            # since the borrower pay correct amount, interest of lines before and after repayment should be same
            ln.update_attributes_for_lines()
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=10000 + ln.calculate_interest(30000)
            )
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
        with freeze_time('2017-3-3'):
            # since the borrower pay correct amount, interest of lines before and after repayment should be same
            ln.update_attributes_for_lines()
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=10000 + ln.calculate_interest(20000)
            )
            ln.update_attributes_for_lines()
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
        with freeze_time('2017-3-4'):
            # since the borrower pay correct amount, interest of lines before and after repayment should be same
            ln.update_attributes_for_lines()
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=10000 + ln.calculate_interest(10000)
            )
            ln.update_attributes_for_lines()
            self.assertEqual(lines.get(date=date(2017, 3, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2017, 3, 3)).interest, ln.calculate_interest(20000))
            self.assertEqual(lines.get(date=date(2017, 3, 4)).interest, ln.calculate_interest(10000))
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2017, 3, 4))

    def test_update_attributes_for_today_and_future_lines_early_and_late(self):
        """
        borrower repay both early and late
        """
        with freeze_time('2016-5-29'):
            ln = LoanFactory(
                loan_amount=45000,
                normal_repayment_amount=5000,
                bullet_repayment_amount=20000,
                loan_interest_rate=2.5,
                loan_interest_type=ACTUAL_365,
                loan_fee=900,
            )
            lines = ln.lines.all()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(35000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower repay early on this day
        with freeze_time('2016-5-30'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(35000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
            # borrower repay thrice the normal amount. So, the borrower pay early for two days
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=15000 + 900 + ln.calculate_interest(45000)
            )
            # check if interest of 2016-5-31 and 2016-6-1 are reduced
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        with freeze_time('2016-5-31'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        with freeze_time('2016-6-1'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower repay normal on this day
        with freeze_time('2016-6-2'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
            # make sure the interest amount is correctly. Interest is total of 3 days.
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(30000) * 3)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=5000 + ln.calculate_interest(30000) * 3
            )
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower do not repay on this day
        with freeze_time('2016-6-3'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower repay late
        with freeze_time('2016-6-4'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if interest for 2016-6-4 is increased
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(25000))
            # make sure total interest is correct. Interest is total of two days.
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(25000) * 2)
            # borrower also repay for yesterday
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=25000 + ln.calculate_interest(25000) * 2
            )
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(25000))
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2016, 6, 4))

    def test_update_attributes_for_today_and_future_lines_early_and_late_partial(self):
        """
        borrower repay both early and late
        but early or late amount not multiple of normal repayment amount
        """
        with freeze_time('2016-5-29'):
            ln = LoanFactory(
                loan_amount=45000,
                normal_repayment_amount=5000,
                bullet_repayment_amount=20000,
                loan_interest_rate=2.5,
                loan_interest_type=ACTUAL_365,
                loan_fee=900,
            )
            lines = ln.lines.all()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(35000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower repay early on this day
        with freeze_time('2016-5-30'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(40000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(35000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
            # borrower repay more than the normal amount.
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=8000 + 900 + ln.calculate_interest(45000)
            )
            # check if interest of 2016-5-31 is reduced
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(35000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        with freeze_time('2016-5-31'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(35000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        with freeze_time('2016-6-1'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(30000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower repay normal on this day
        with freeze_time('2016-6-2'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
            # make sure the interest amount is correctly. Interest is total of 3 days.
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(37000) * 3)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=5000 + ln.calculate_interest(37000) * 3
            )
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(25000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))
        # borrower do not repay on this day
        with freeze_time('2016-6-3'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(32000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(20000))

        # borrower repay late
        with freeze_time('2016-6-4'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if interest for 2016-6-4 is increased
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(32000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(32000))
            # make sure total interest is correct. Interest is total of two days.
            self.assertEqual(ln.interest_outstanding, ln.calculate_interest(32000) * 2)
            # borrower also repay for past days
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=32000 + ln.calculate_interest(32000) * 2
            )
            ln.update_attributes_for_lines()
            # check if assumed interest are correct
            self.assertEqual(lines.get(date=date(2016, 5, 30)).interest, ln.calculate_interest(45000))
            self.assertEqual(lines.get(date=date(2016, 5, 31)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 1)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 2)).interest, ln.calculate_interest(37000))
            self.assertEqual(lines.get(date=date(2016, 6, 3)).interest, ln.calculate_interest(32000))
            self.assertEqual(lines.get(date=date(2016, 6, 4)).interest, ln.calculate_interest(32000))
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2016, 6, 4))

    def test_update_attributes_for_lines_for_equal_repayments_normal(self):
        """
        check if update_attributes_for_lines work for loan_interest_type EQUAL_REPAYMENTS
        test case is perfect case where borrower repay perfectly
        """
        # test case 1
        normal_repay_amount = Loan.calculate_equal_repayment_amount_for_loan_initialization(60000, 2.5 * 10, 3)
        # magic number is from excel
        self.assertEqual(normal_repay_amount, 20330)
        with freeze_time('2017-5-11'):
            ln = LoanFactory(
                loan_amount=60000,
                number_of_repayments=3,
                normal_repayment_amount=normal_repay_amount,
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=2.5 * 10,
                loan_fee=1000
            )
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 12)).principal, 19837)
            self.assertEqual(lines.get(date=date(2017, 5, 12)).interest, 493)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).principal, 19999)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).interest, 331)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).principal, 20164)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).interest, 166)
        with freeze_time('2017-5-12'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 12)).principal, 19837)
            self.assertEqual(lines.get(date=date(2017, 5, 12)).interest, 493)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).principal, 19999)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).interest, 331)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).principal, 20164)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).interest, 166)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount + 1000
            )
        with freeze_time('2017-5-13'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 12)).principal, 19837)
            self.assertEqual(lines.get(date=date(2017, 5, 12)).interest, 493)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).principal, 19999)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).interest, 331)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).principal, 20164)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).interest, 166)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-14'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 12)).principal, 19837)
            self.assertEqual(lines.get(date=date(2017, 5, 12)).interest, 493)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).principal, 19999)
            self.assertEqual(lines.get(date=date(2017, 5, 13)).interest, 331)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).principal, 20164)
            self.assertEqual(lines.get(date=date(2017, 5, 14)).interest, 166)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2017, 5, 14))

        # test case 2
        normal_repay_amount = Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5 * 7, 5)
        # magic number is from excel
        self.assertEqual(normal_repay_amount, 4069)
        with freeze_time('2017-5-25'):
            ln = LoanFactory(
                loan_amount=20000,
                number_of_repayments=5,
                normal_repayment_amount=normal_repay_amount,
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=17.5,
                loan_fee=0
            )
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-26'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-27'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-28'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-29'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-30'):
            # loan schedule line are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2017, 5, 30))

    def test_update_attributes_for_lines_for_equal_repayments_early(self):
        # test case 1 - early repayment on first day
        # FIXME: having numerical accuracy error. Normal repayment is 3308 or 3309
        normal_repay_amount = Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5 * 7, 5)
        # magic number is from excel
        self.assertEqual(normal_repay_amount, 4069)
        with freeze_time('2017-5-25'):
            ln = LoanFactory(
                loan_amount=20000,
                number_of_repayments=5,
                normal_repayment_amount=normal_repay_amount,
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=17.5,
                loan_fee=0
            )
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-26'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            # early repayment
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount + 3000
            )
            # magic numbers are from excel
            # data are not updated yet. Those will be updated tomorrow
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-27'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            # all lines data are updated.
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 6954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3233)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 75)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 3253)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 56)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 3270)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 38)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 3290)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 19)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=3309
            )
        with freeze_time('2017-5-28'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=3308
            )
        with freeze_time('2017-5-29'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=3308
            )
        with freeze_time('2017-5-30'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=3308
            )
            # FIXME: loan close should be tested. But numerical error here

        # test case 2 - early repayment on third day
        normal_repay_amount = Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5 * 7, 5)
        # magic number is from excel
        self.assertEqual(normal_repay_amount, 4069)
        with freeze_time('2017-5-25'):
            ln = LoanFactory(
                loan_amount=20000,
                number_of_repayments=5,
                normal_repayment_amount=normal_repay_amount,
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=17.5,
                loan_fee=0
            )
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-26'):
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-27'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-28'):
            ln.update_attributes_for_lines()
            # this is early repayment.
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount + 3000
            )
            # data are not updated yet. Those will be updated tomorrow
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-29'):
            ln.update_attributes_for_lines()
            # values of 2017-5-28, 2017-5-29 and 2017-5-30 should be changed because of early repayment from yesterday
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 7000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 2527)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 29)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 2542)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 14)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=2556
            )
        with freeze_time('2017-5-30'):
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=2556
            )
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2017, 5, 30))

    def test_update_attributes_for_lines_for_equal_repayments_late(self):
        """
        Test cases and magic numbers are from Miranda excel
        """
        # test case 1 - lower than normal amount on first day
        normal_repay_amount = Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5 * 7, 5)
        # magic number is from excel
        self.assertEqual(normal_repay_amount, 4069)
        with freeze_time('2017-5-25'):
            ln = LoanFactory(
                loan_amount=20000,
                number_of_repayments=5,
                normal_repayment_amount=normal_repay_amount,
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=17.5,
                loan_fee=0
            )
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-26'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            # only give 3500 less than normal amount. So, late repayment
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount - 3500
            )
            # magic numbers are from excel
            # not updated yet. Those will be updated tomorrow
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-27'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            # magic numbers are from excel
            # values of every lines are updated
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 454)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 4845)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 112)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4872)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 85)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4900)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 57)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4929)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 28)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=4957
            )
        with freeze_time('2017-5-28'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=4957
            )
        with freeze_time('2017-5-29'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=4957
            )
        with freeze_time('2017-5-30'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=4957
            )
            # check if loan is closed
            self.assertEqual(ln.repaid_on, date(2017, 5, 30))

        # test case 2 - late repayment on third day
        # FIXME: numerical accuracy error. normal repayment varying between 5833 or 5834
        normal_repay_amount = Loan.calculate_equal_repayment_amount_for_loan_initialization(20000, 2.5 * 7, 5)
        # magic number is from excel
        self.assertEqual(normal_repay_amount, 4069)
        with freeze_time('2017-5-25'):
            ln = LoanFactory(
                loan_amount=20000,
                number_of_repayments=5,
                normal_repayment_amount=normal_repay_amount,
                bullet_repayment_amount=0,
                loan_interest_type=EQUAL_REPAYMENTS,
                loan_interest_rate=17.5,
                loan_fee=0
            )
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # magic numbers are from excel
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-26'):
            # lines are updated daily
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-27'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount
            )
        with freeze_time('2017-5-28'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=normal_repay_amount - 3500
            )
            # magic numbers are from excel
            # data are not updated yet. Those should be updated tomorrow
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 4000)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 4023)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 46)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 4046)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 23)
        with freeze_time('2017-5-29'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            lines = ln.lines.all()
            # check if assumed principal and interest are correct
            # values of 2017-5-28, 2017-5-29 and 2017-5-30 are updated
            self.assertEqual(lines.get(date=date(2017, 5, 26)).principal, 3954)
            self.assertEqual(lines.get(date=date(2017, 5, 26)).interest, 115)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).principal, 3977)
            self.assertEqual(lines.get(date=date(2017, 5, 27)).interest, 92)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).principal, 500)
            self.assertEqual(lines.get(date=date(2017, 5, 28)).interest, 69)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).principal, 5768)
            self.assertEqual(lines.get(date=date(2017, 5, 29)).interest, 67)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).principal, 5801)
            self.assertEqual(lines.get(date=date(2017, 5, 30)).interest, 33)
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=5834
            )
        with freeze_time('2017-5-30'):
            # lines are updated daily
            ln.update_attributes_for_lines()
            Repayment.objects.create(
                loan=ln,
                date=date.today(),
                amount=5834
            )
            # FIXME: loan close should be tested. But numerical error here


class LoanAPITests(APITestCase):
    """
    Test API calls to make sure nothing is broken.
    """

    def setUp(self):
        c = CurrencyFactory()
        self.auto_id = Faker()

    def test_recording_repayment(self):
        initial_date = date(2016, 10, 31)
        with freeze_time(initial_date):
            loan = LoanFactory(
                loan_amount=10000,
                normal_repayment_amount=1000,
                bullet_repayment_amount=1000,
                loan_fee=200
            )
        # POST a repayment for this loan
        reason = ReasonForDelayedRepayment(
            reason_en='some reason',
            reason_mm=''
        )
        reason.save()
        r = Repayment(
            id=self.auto_id.random_number(),
            loan=loan,
            date=initial_date + timedelta(days=1),
            amount=1200,
            reason_for_delay=reason,
        )
        self.client.force_authenticate(user=loan.borrower.agent.user)
        response = self.client.post('/api/v1/repayments/', RepaymentSerializer(r).data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Repayment.objects.count(), 1)
        saved_rep = Repayment.objects.first()
        self.assertEqual(saved_rep.recorded_by, loan.borrower.agent.user)

        r = Repayment(
            id=self.auto_id.random_number(),
            loan=loan,
            date=initial_date + timedelta(days=1),
            amount=1200,
            reason_for_delay=reason,
        )
        response = self.client.post('/api/v1/repayments/', RepaymentSerializer(r).data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Repayment.objects.count(), 2)

        r = Repayment(
            id=self.auto_id.random_number(),
            loan=loan,
            date=initial_date + timedelta(days=3),
            amount=1000,
            reason_for_delay=reason,
        )
        response = self.client.put('/api/v1/repayments/', RepaymentSerializer(r).data)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_submit_loan_request(self):
        """
        Upload a loan request through the API and check that everything went well.
        Useful to test nested data for lines.
        """
        b = BorrowerFactory()
        u = b.agent.user
        # grant permissions to the user so the requests can pass through
        permission = Permission.objects.get(codename='zw_request_loan')
        u.user_permissions.add(permission)
        permission = Permission.objects.get(codename='change_loan')
        u.user_permissions.add(permission)
        c = Currency.objects.first()

        with freeze_time(date(2017, 2, 6)):
            # set "server side" time
            self.client.force_authenticate(user=b.agent.user)

        self.assertEqual(Currency.objects.count(), 1)

        image = Image.new('RGB', (100, 100))
        tmp_file = tempfile.NamedTemporaryFile(suffix='.jpg')
        image.save(tmp_file, 'JPEG')

        # loan_contract_photo below is commented out as it takes a while to upload a photo to S3
        # but the code is left there as an example.
        with open(tmp_file.name, 'rb') as fp:
            b64img = base64.b64encode(fp.read())
            # FIXME: we've removed some items for now, as null didn't go down too well
            loan_data = {
                "lines": [
                    {
                        "date": "2017-02-07",
                        "principal": "2000.00",
                        "fee": "0.00",
                        "interest": "0.00",
                        "penalty": "0.00"
                    },
                    {
                        "date": "2017-02-08",
                        "principal": "2000.00",
                        "fee": "0.00",
                        "interest": "0.00",
                        "penalty": "0.00"
                    },
                    {
                        "date": "2017-02-09",
                        "principal": "2000.00",
                        "fee": "0.00",
                        "interest": "0.00",
                        "penalty": "0.00"
                    },
                    {
                        "date": "2017-02-10",
                        "principal": "2000.00",
                        "fee": "0.00",
                        "interest": "0.00",
                        "penalty": "0.00"
                    }
                ],
                "repayments": [],
                "contract_number": "123456789012",
                "state": 'draft',
                "loan_amount": "8000.00",
                "loan_interest": "0.00",
                "loan_fee": "0.00",
                "late_penalty_fee": "0.00",
                "late_penalty_per_x_days": 7,
                "late_penalty_max_days": 70,
                "prepayment_penalty": "0.00",
                "number_of_repayments": 0,
                "normal_repayment_amount": "2000.00",
                "bullet_repayment_amount": "2000.00",
                # "loan_contract_photo": b64img,
                "repaid_on": None,
                "effective_interest_rate": None,
                "pilot": "",
                "expect_sales_growth": "null",
                "comments": "",
                "borrower": b.pk,
                "guarantor": b.pk,
                "loan_currency": c.pk,
                "purpose": None,
            }
            # upload as JSON, as multipart does not support nested data (at least for tests?)
            response = self.client.post('/api/v1/loans/', loan_data, format='json')
            # check that the loan was created properly
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            loan_from_server = response.data
            self.assertEqual(Loan.objects.count(), 1)
            self.assertEqual(RepaymentScheduleLine.objects.count(), 4)
            loan = Loan.objects.first()
            self.assertEqual(loan.total_outstanding, 8000)
            self.assertEqual(loan.state, LOAN_REQUEST_SUBMITTED)
            loan_data_put = {
                "lines": [],
                "repayments": [],
                "borrower": b.pk,
            }
            # check that a PUT request without lines is rejected
            response = self.client.put('/api/v1/loans/{pk}/'.format(pk=loan.pk), loan_data_put, format="json")
            self.assertEqual(response.content, b'["Invalid PUT request"]')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

            # make a valid PUT request, it must go through
            fee1 = 50
            fee2 = 10
            loan_from_server['state'] = 'submitted'
            loan_from_server.pop('loan_contract_photo', [])  # remove the key, as it cannot be sent null
            loan_from_server['lines'][0]['fee'] = fee1
            newline = {
                "date": "2017-02-11",
                "principal": "0.00",
                "fee": fee2,
                "interest": "0.00",
                "penalty": "0.00"
            }
            loan_from_server['lines'].append(newline)
            response = self.client.put('/api/v1/loans/{pk}/'.format(pk=loan.pk), loan_from_server, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(Loan.objects.count(), 1)
            self.assertEqual(RepaymentScheduleLine.objects.count(), 5)
            # the next line fails if we don't freeze_time when initializing the test client
            self.assertEqual(RepaymentScheduleLine.objects.filter(date='2017-02-07')[0].fee, fee1)
            self.assertEqual(RepaymentScheduleLine.objects.filter(date='2017-02-11')[0].fee, fee2)
            loan = Loan.objects.first()
            self.assertEqual(loan.state, 'submitted')
        # try a PATCH request on the loan status
        response = self.client.patch('/api/v1/loans/{pk}/'.format(pk=loan.pk), {"state": "disbursed", "lines": []}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Loan.objects.count(), 1)
        self.assertEqual(RepaymentScheduleLine.objects.count(), 5)
        loan = Loan.objects.first()
        self.assertEqual(loan.state, 'disbursed')

    def test_approve_loan_approved(self):
        """
        test LoanViewSet approve api endpoint
        """
        loan = LoanFactory(state=LOAN_REQUEST_SIGNED)
        user = loan.borrower.agent.user
        self.client.force_authenticate(user=user)
        data = {
            "loan": loan.pk,
            "approved": True,
            "comments": "approved in testing"
        }
        response = self.client.post('/api/v1/loans/' + str(loan.id) + '/approve/', data, format='json')
        # user don't have `zw_approve_loan` permission yet
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        permission = Permission.objects.get(codename='zw_approve_loan')
        user.user_permissions.add(permission)
        # because of cache, user is not updated with new permissions yet. refetch user from db and relogin to solve this.
        user = User.objects.get(pk=user.pk)
        self.client.force_authenticate(user=user)
        response = self.client.post('/api/v1/loans/' + str(loan.id) + '/approve/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['result'], LOAN_REQUEST_APPROVED)

        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_REQUEST_APPROVED)
        self.assertEqual(LoanRequestReview.objects.latest('id').reviewer, user)

    def test_approve_loan_rejected(self):
        """
        test LoanViewSet approve api endpoint
        """
        loan = LoanFactory(state=LOAN_REQUEST_SIGNED)
        agent = loan.borrower.agent
        user = agent.user
        loan2 = LoanFactory(borrower__agent=agent)
        self.client.force_authenticate(user=user)
        data = {
            "loan": loan.pk,
            "approved": False,
            "comments": "consider this alternative loan",
            "alternative_offer": loan2.pk,
        }
        response = self.client.post('/api/v1/loans/' + str(loan.id) + '/approve/', data, format='json')
        # user don't have `zw_approve_loan` permission yet
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        permission = Permission.objects.get(codename='zw_approve_loan')
        user.user_permissions.add(permission)
        # because of cache, user is not updated with new permissions yet. refetch user from db and relogin to solve this.
        user = User.objects.get(pk=user.pk)
        self.client.force_authenticate(user=user)
        response = self.client.post('/api/v1/loans/' + str(loan.id) + '/approve/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['result'], LOAN_REQUEST_REJECTED)

        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_REQUEST_REJECTED)
        self.assertEqual(LoanRequestReview.objects.latest('id').reviewer, user)

    def test_approve_loan_approved_usingGraphql(self):
        """
        test LoanViewSet approve api endpoint
        """
        loan = LoanFactory(state=LOAN_REQUEST_SIGNED)
        agent = loan.borrower.agent
        user = agent.user
        token, created = Token.objects.get_or_create(user=user)

        data = dict(reviewer=user.id,
                    comments='"consider this alternative loan"',
                    approved='true',
                    loan=loan.id)
        template = Template('reviewer: $reviewer, \
                                comments: $comments, \
                                approved: $approved, \
                                loan:$loan').safe_substitute(data)
        query = {"query": "mutation {approveLoanReview (%s) {id} }" % (template)}
        response = self.client.post('/graphql/', query, HTTP_AUTHORIZATION='Token {}'.format(token))
        # user don't have `zw_approve_loan` permission yet
        detail = json.loads(response._container[0].decode())
        self.assertEqual(detail, {"data": {"approveLoanReview": {"id": None}}})
        permission = Permission.objects.get(codename='zw_approve_loan')
        user.user_permissions.add(permission)
        # because of cache, user is not updated with new permissions yet.
        # refetch user from db and relogin to solve this.
        user = User.objects.get(pk=user.pk)
        response = self.client.post('/graphql/', query, HTTP_AUTHORIZATION='Token {}'.format(token))
        container = json.loads(response._container[0].decode())
        approveLoan = container['data']['approveLoanReview']['id']
        loan_request = LoanRequestReview.objects.get(loan=loan).id
        self.assertEqual(approveLoan, loan_request)
        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_REQUEST_APPROVED)
        self.assertEqual(LoanRequestReview.objects.latest('id').reviewer, user)

    def test_approve_loan_rejected_using_Graphql(self):
        """
        test LoanViewSet approve api endpoint
        """
        loan = LoanFactory(state=LOAN_REQUEST_SIGNED)
        agent = loan.borrower.agent
        loan2 = LoanFactory(borrower__agent=agent)
        user = agent.user
        token, created = Token.objects.get_or_create(user=user)
        data = dict(offer=loan2.id, reviewer=user.id,
                    comments='"consider this alternative loan"',
                    approved='false',
                    loan=loan.id)
        template = Template('alternativeOffer:$offer,\
                                reviewer:$reviewer,\
                                comments:$comments\
                                approved:$approved,\
                                loan:$loan').safe_substitute(data)
        query = {"query": "mutation {approveLoanReview (%s){id}}" % (template)}
        response = self.client.post('/graphql/', query, HTTP_AUTHORIZATION='Token {}'.format(token))
        # user don't have `zw_approve_loan` permission yet
        detail = json.loads(response._container[0].decode())
        self.assertEqual(detail, {"data": {"approveLoanReview": {"id": None}}})
        permission = Permission.objects.get(codename='zw_approve_loan')
        user.user_permissions.add(permission)
        # because of cache, user is not updated with new permissions yet.
        # refetch user from db and relogin to solve this.
        user = User.objects.get(pk=user.pk)
        response = self.client.post('/graphql/', query, HTTP_AUTHORIZATION='Token {}'.format(token))
        loan_request = LoanRequestReview.objects.get(loan=loan).id
        container = json.loads(response._container[0].decode())
        approveLoan = container['data']['approveLoanReview']['id']
        self.assertEqual(approveLoan, loan_request)
        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_REQUEST_REJECTED)
        self.assertEqual(LoanRequestReview.objects.latest('id').reviewer, user)


class DisbursementTests(TestCase):
    """
    Currently test cases are only for custom save
    """
    # def test_set_transaction_id_for_cash_at_agent(self):
    #     """
    #     for DISB_METHOD_CASH_AT_AGENT method,
    #     when state is DISBURSEMENT_SENT
    #     provider_transaction_id should be set to
    #     'disbursed by method which do not use this field'
    #     """
    #     d = DisbursementFactory(method=DISB_METHOD_CASH_AT_AGENT)
    #     self.assertIs(d.provider_transaction_id, None)
    #     d.transfer = TransferFactory()
    #     d.state = DISBURSEMENT_SENT
    #     d.save()
    #     self.assertEqual(d.provider_transaction_id, 'disbursed by method which do not use this field')

    # def test_validation_error_for_kbz(self):
    #     """
    #     for DISB_METHOD_KBZ_GENERIC or DISB_METHOD_WAVE_P2P,
    #     when state is DISBURSEMENT_SENT
    #     provider_transaction_id should not be null
    #     """
    #     d = DisbursementFactory(method=DISB_METHOD_KBZ_GENERIC)
    #     self.assertIs(d.provider_transaction_id, None)
    #     # for now, Transfer only contain minimum data just to do testing
    #     # in fact, it should contain corresponding data according to the object it is pointed by
    #     # for e.g. method should be the same
    #     d.transfer = TransferFactory()
    #     d.state = DISBURSEMENT_SENT
    #     # test if error is raised
    #     # https://stackoverflow.com/questions/16214846/test-if-validationerror-was-raised
    #     self.assertRaises(ValidationError, d.save)

    # def test_no_validation_error_for_kbz(self):
    #     """
    #     for DISB_METHOD_KBZ_GENERIC or DISB_METHOD_WAVE_P2P,
    #     when state is DISBURSEMENT_SENT
    #     provider_transaction_id should not be null
    #     """
    #     d = DisbursementFactory(method=DISB_METHOD_KBZ_GENERIC)
    #     self.assertIs(d.provider_transaction_id, None)
    #     # FIXME: this is transaction id of wave. change it with proper transaction id for kbz
    #     d.provider_transaction_id = '1082715985'
    #     d.transfer = TransferFactory()
    #     d.state = DISBURSEMENT_SENT
    #     # check if no error is raised
    #     # https://stackoverflow.com/questions/4319825/python-unittest-opposite-of-assertraises
    #     try:
    #         d.save()
    #     except ValidationError:
    #         self.fail('Validation Error is raised')

    # def test_validation_error_for_wave(self):
    #     """
    #     for DISB_METHOD_KBZ_GENERIC or DISB_METHOD_WAVE_P2P,
    #     when state is DISBURSEMENT_SENT
    #     provider_transaction_id should not be null
    #     """
    #     d = DisbursementFactory(method=DISB_METHOD_WAVE_P2P)
    #     self.assertIs(d.provider_transaction_id, None)
    #     d.transfer = TransferFactory()
    #     d.state = DISBURSEMENT_SENT
    #     # test if error is raised
    #     # https://stackoverflow.com/questions/16214846/test-if-validationerror-was-raised
    #     self.assertRaises(ValidationError, d.save)

    # def test_no_validation_error_for_wave(self):
    #     """
    #     for DISB_METHOD_KBZ_GENERIC or DISB_METHOD_WAVE_P2P,
    #     when state is DISBURSEMENT_SENT
    #     provider_transaction_id should not be null
    #     """
    #     d = DisbursementFactory(method=DISB_METHOD_WAVE_P2P)
    #     self.assertIs(d.provider_transaction_id, None)
    #     d.provider_transaction_id = '1082715985'
    #     d.transfer = TransferFactory()
    #     d.state = DISBURSEMENT_SENT
    #     # check if no error is raised
    #     # https://stackoverflow.com/questions/4319825/python-unittest-opposite-of-assertraises
    #     try:
    #         d.save()
    #     except ValidationError:
    #         self.fail('Validation Error is raised')

    # def test_transfer_error(self):
    #     """
    #     when state is DISBURSEMENT_SENT
    #     transfer should not be null
    #     Transfer is missing in this test case
    #     """
    #     d = DisbursementFactory(method=DISB_METHOD_CASH_AT_AGENT)
    #     self.assertIs(d.provider_transaction_id, None)
    #     d.state = DISBURSEMENT_SENT
    #     # test if error is raised
    #     # https://stackoverflow.com/questions/16214846/test-if-validationerror-was-raised
    #     self.assertRaises(ValidationError, d.save)

    # def test_fee_not_repaid_error(self):
    #     loan1 = LoanFactory(loan_fee=500, state=LOAN_REQUEST_APPROVED)
    #     loan2 = LoanFactory(loan_fee=500, state=LOAN_REQUEST_APPROVED)
    #     disb = DisbursementFactory()
    #     loan1.disbursement = disb
    #     loan1.save()
    #     loan2.disbursement = disb
    #     loan2.save()

    #     RepaymentFactory(amount=500, loan=loan1)

    #     disb.transfer = TransferFactory()
    #     disb.state = DISBURSEMENT_SENT
    #     # loan2 is missing fee
    #     self.assertRaises(FeeNotPaidError, disb.save)

    #     # with atomic transaction, both loans' state will no be changed
    #     loan1.refresh_from_db()
    #     loan2.refresh_from_db()
    #     self.assertEqual(loan1.state, LOAN_REQUEST_APPROVED)
    #     self.assertEqual(loan2.state, LOAN_REQUEST_APPROVED)

    #     RepaymentFactory(amount=500, loan=loan2)
    #     # check if no error is raised
    #     try:
    #         disb.save()
    #     except FeeNotPaidError:
    #         self.fail('FeeNotPaidError is raised')

    #     loan1.refresh_from_db()
    #     loan2.refresh_from_db()
    #     self.assertEqual(loan1.state, LOAN_DISBURSED)
    #     self.assertEqual(loan2.state, LOAN_DISBURSED)


class DisbursementAPITests(APITestCase):
    """
    POST disbursement through API and check that everything went well
    """

    def test_post_disbursement(self):
        """
        Insanity check to test disbursement api
        """
        a1 = AgentFactory()
        l1 = LoanFactory(loan_fee=0, state=LOAN_REQUEST_APPROVED)
        log_in_user = User.objects.get(username=a1.user)
        self.client.force_authenticate(user=log_in_user)
        data = {
            "method": 3,
            "amount": "60000.00",
            "disbursed_to": a1.pk,
            "fees_paid": 0,
            "details": {
                "loans": [l1.pk]
            },
            "loans_disbursed": [l1.pk]
        }
        response = self.client.post('/api/v1/disbursements/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # def test_disbursement_methods(self):
    #     """
    #     Disbursement POST testing with different methods
    #     """
    #     a = AgentFactory.create_batch(2)
    #     l = LoanFactory.create_batch(3)
    #     import pdb;pdb.set_trace()
    #     # need to log in. Otherwise, POST request will have authentication error
    #     log_in_user = User.objects.get(username=a[0].user)
    #     self.client.force_authenticate(user=log_in_user)

    #     # disbursement via wave
    #     data = {
    #         "method": 1,
    #         "amount": "30000.00",
    #         "fees_paid": 0,
    #         "disbursed_to": a[0].pk,
    #         "details": {
    #             "loans": [l[0].pk]
    #         },
    #         "loans_disbursed": [l[0].pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_201_CREATED)
    #     self.assertEqual(Disbursement.objects.latest('id').method, DISB_METHOD_WAVE_TRANSFER)
    #     self.assertEqual(Disbursement.objects.count(), 1)
    #     # self.assertEqual(Disbursement.objects.latest('id').disbursed_by, log_in_user)
    #     Loan.refresh_from_db(l[0])
    #     self.assertEqual(Disbursement.objects.latest('id').pk, l[0].disbursement.pk)

    #     # disbursement via bank
    #     data = {
    #         "provider_transaction_id": "198345729845ef",
    #         "method": 2,
    #         "amount": "230000.00",
    #         "fees_paid": 0,
    #         "disbursed_to": a[0].pk,
    #         "details": {
    #             "loans": [l[0].pk]
    #         },
    #         "loans_disbursed": [l[0].pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_201_CREATED)
    #     self.assertEqual(Disbursement.objects.latest('id').method, DISB_METHOD_BANK_TRANSFER)
    #     self.assertEqual(Disbursement.objects.count(), 2)
    #     # self.assertEqual(Disbursement.objects.latest('id').disbursed_by, log_in_user)
    #     Loan.refresh_from_db(l[0])
    #     self.assertEqual(Disbursement.objects.latest('id').pk, l[0].disbursement.pk)

    #     # disbursement via wave money
    #     data = {
    #         "method": 3,
    #         "amount": "60000.00",
    #         "fees_paid": 0,
    #         "disbursed_to": a[1].pk,
    #         "details": {
    #             "loans": [l[1].pk, l[2].pk]
    #         },
    #         "loans_disbursed": [l[1].pk, l[2].pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_201_CREATED)
    #     self.assertEqual(Disbursement.objects.latest('id').method, DISB_METHOD_WAVE_N_CASH_OUT)
    #     self.assertEqual(Disbursement.objects.count(), 3)
    #     # self.assertEqual(Disbursement.objects.latest('id').disbursed_by, log_in_user)
    #     Loan.refresh_from_db(l[1])
    #     self.assertEqual(Disbursement.objects.latest('id').pk, l[1].disbursement.pk)
    #     Loan.refresh_from_db(l[2])
    #     self.assertEqual(Disbursement.objects.latest('id').pk, l[2].disbursement.pk)

    #     # method 4 do not exist
    #     data = {
    #         "method": 4,
    #         "amount": "60000.00",
    #         "disbursed_to": a[1].pk,
    #         "details": {
    #             "recipient_number": "099667345627",
    #             "sender_number": "097456123543"
    #         },
    #         "loans_disbursed": [l[1].pk, l[2].pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    #     self.assertListEqual(response.data['method'], ['"4" is not a valid choice.'])
    #     self.assertEqual(Disbursement.objects.count(), 3)

    # def test_invalid_phone_numbers(self):
    #     """
    #     Check validation of phone numbers in details field of input json
    #     """
    #     a1 = AgentFactory()
    #     l1 = LoanFactory()
    #     # need to log in. Otherwise, POST request will have authentication error
    #     log_in_user = User.objects.get(username=a1.user)
    #     self.client.force_authenticate(user=log_in_user)

    #     # disbursement via kbz with recipient number missing
    #     data = {
    #         "provider_transaction_id": "198345729845ef",
    #         "method": 2,
    #         "amount": "230000.00",
    #         "disbursed_to": a1.pk,
    #         "details": {
    #             "recipient_name": "Ma Khine",
    #             "recipient_NRC_number": "1/N/LaPaTha-123456"
    #         },
    #         "loans_disbursed": [l1.pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    #     self.assertListEqual(response.data['non_field_errors'], ['Phone Number Missing'])
    #     Loan.refresh_from_db(l1)
    #     self.assertEqual(l1.disbursement, None)

    #     # disbursement via kbz with invalid phone number
    #     ph_no = "099667345627"
    #     # assert if phone number is invalid
    #     self.assertFalse(pn.is_valid_number_for_region(pn.parse(ph_no, "MM"), "MM"))
    #     data = {
    #         "provider_transaction_id": "198345729845ef",
    #         "method": 2,
    #         "amount": "230000.00",
    #         "disbursed_to": a1.pk,
    #         "details": {
    #             "recipient_name": "Ma Khine",
    #             "recipient_phone_number": ph_no,
    #             "recipient_NRC_number": "1/N/LaPaTha-123456"
    #         },
    #         "loans_disbursed": [l1.pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    #     self.assertListEqual(response.data['non_field_errors'], ['Phone Number Invalid'])

    #     # disbursement via wave money but numbers are not telenor
    #     ph_no_1 = "09260533015"
    #     self.assertTrue(pn.is_valid_number_for_region(pn.parse(ph_no_1, "MM"), "MM"))
    #     ph_no_2 = "09962309982"
    #     self.assertTrue(pn.is_valid_number_for_region(pn.parse(ph_no_2, "MM"), "MM"))
    #     data = {
    #         "method": 3,
    #         "amount": "60000.00",
    #         "disbursed_to": a1.pk,
    #         "details": {
    #             "recipient_number": ph_no_1,
    #             "sender_number": ph_no_2
    #         },
    #         "loans_disbursed": [l1.pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    #     self.assertListEqual(response.data['non_field_errors'], ['Phone Number Invalid'])

    #     # disbursement via wave money but one number is not telenor
    #     ph_no_1 = "09795722081"
    #     self.assertTrue(pn.is_valid_number_for_region(pn.parse(ph_no_1, "MM"), "MM"))
    #     # this second number is not telenor (telenor start with 097)
    #     ph_no_2 = "09962309982"
    #     self.assertTrue(pn.is_valid_number_for_region(pn.parse(ph_no_2, "MM"), "MM"))
    #     data = {
    #         "method": 3,
    #         "amount": "60000.00",
    #         "disbursed_to": a1.pk,
    #         "details": {
    #             "recipient_number": ph_no_1,
    #             "sender_number": ph_no_2
    #         },
    #         "loans_disbursed": [l1.pk]
    #     }
    #     response = self.client.post('/api/v1/disbursements/', data, format='json')
    #     self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    #     self.assertListEqual(response.data['non_field_errors'], ['Phone Number Invalid'])

    def test_disburse_endpoint(self):
        """
        test disbursement disburse API endpoint
        """
        # necessary objects
        agent = AgentFactory()
        loan = LoanFactory(loan_fee=200, state=LOAN_REQUEST_APPROVED)
        # transfer = TransferFactory()
        disbursement = DisbursementFactory()

        # log in user
        log_in_user = User.objects.get(username=agent.user)
        self.client.force_authenticate(user=log_in_user)

        # loan FK to disbursement
        loan.disbursement = disbursement
        loan.save()

        # pay loan fee
        RepaymentFactory(amount=200, loan=loan)

        # data = {
        #     "provider_transaction_id": 31459,
        #     "transfer": transfer.id,
        # }
        # data = {}

        response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', format='json')
        # check for permission
        # user don't have `zw_disburse_loan` permission yet
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        permission = Permission.objects.get(codename='zw_disburse_loan')
        log_in_user.user_permissions.add(permission)
        # because of cache, user is not updated with new permissions yet. refetch user from db and relogin to solve this.
        user = User.objects.get(pk=log_in_user.pk)
        self.client.force_authenticate(user=user)
        response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # check state change
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.state, DISBURSEMENT_SENT)
        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_DISBURSED)

    def test_disburse_endpoint_for_wave_transfer(self):
        """
        test disbursement disburse API endpoint for method DISB_METHOD_WAVE_TRANSFER
        """
        # necessary objects
        agent = AgentFactory()
        loan = LoanFactory(loan_fee=200, state=LOAN_REQUEST_APPROVED)
        # transfer = TransferFactory()
        disbursement = DisbursementFactory()

        # log in user
        permission = Permission.objects.get(codename='zw_disburse_loan')
        agent.user.user_permissions.add(permission)
        log_in_user = User.objects.get(username=agent.user)
        self.client.force_authenticate(user=log_in_user)

        # loan FK to disbursement
        loan.disbursement = disbursement
        loan.save()

        # pay loan fee
        RepaymentFactory(amount=200, loan=loan)

        # transfer missing (for DISB_METHOD_WAVE_TRANSFER method, only transfer is needed)
        # data = {
        # }
        # response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', data, format='json')
        # self.assertListEqual(response.data['non_field_errors'], ['Transfer Missing'])

        # state should not be changed
        # disbursement.refresh_from_db()
        # self.assertEqual(disbursement.state, DISBURSEMENT_REQUESTED)
        # loan.refresh_from_db()
        # self.assertEqual(loan.state, LOAN_REQUEST_APPROVED)

        # data = {
        #     "transfer": transfer.id,
        # }
        response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # check state change
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.state, DISBURSEMENT_SENT)
        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_DISBURSED)

    def test_disburse_endpoint_for_bank_transfer(self):
        """
        test disbursement disburse API endpoint for method DISB_METHOD_BANK_TRANSFER
        """
        # necessary objects
        agent = AgentFactory()
        loan = LoanFactory(loan_fee=200, state=LOAN_REQUEST_APPROVED)
        # transfer = TransferFactory()
        disbursement = DisbursementFactory(method=DISB_METHOD_BANK_TRANSFER)

        # log in user
        permission = Permission.objects.get(codename='zw_disburse_loan')
        agent.user.user_permissions.add(permission)
        log_in_user = User.objects.get(username=agent.user)
        self.client.force_authenticate(user=log_in_user)

        # loan FK to disbursement
        loan.disbursement = disbursement
        loan.save()

        # pay loan fee
        RepaymentFactory(amount=200, loan=loan)

        # missing transaction id
        # data = {
        #     "transfer": transfer.id,
        # }
        # response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', data, format='json')
        # self.assertListEqual(response.data['non_field_errors'], ['Transaction Id Missing'])

        # missing transfer
        # data = {
        #     "provider_transaction_id": 31459,
        # }
        # response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', data, format='json')
        # self.assertListEqual(response.data['non_field_errors'], ['Transfer Missing'])

        # state should not be changed
        # disbursement.refresh_from_db()
        # self.assertEqual(disbursement.state, DISBURSEMENT_REQUESTED)
        # loan.refresh_from_db()
        # self.assertEqual(loan.state, LOAN_REQUEST_APPROVED)

        # data = {
        #     "provider_transaction_id": 31459,
        #     "transfer": transfer.id,
        # }
        response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # check state change
        disbursement.refresh_from_db()
        self.assertEqual(disbursement.state, DISBURSEMENT_SENT)
        loan.refresh_from_db()
        self.assertEqual(loan.state, LOAN_DISBURSED)

    def test_disbursement_endpoints(self):
        """
        insanity test to check disbursement API endpoints
        """
        # necessary objects
        agent = AgentFactory()
        loan = LoanFactory(loan_fee=0, state=LOAN_REQUEST_APPROVED)
        # transfer = TransferFactory()

        # log in user
        permission = Permission.objects.get(codename='zw_disburse_loan')
        agent.user.user_permissions.add(permission)
        log_in_user = User.objects.get(username=agent.user)
        self.client.force_authenticate(user=log_in_user)

        # disbursement request via wave money
        data = {
            "method": 3,
            "amount": "60000.00",
            "disbursed_to": agent.pk,
            "fees_paid": 0,
            "details": {
                "loans": [loan.pk],
            },
            "loans_disbursed": [loan.pk]
        }
        response = self.client.post('/api/v1/disbursements/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # # get just created disbursement
        # disbursement = Disbursement.objects.get(pk=response.data['id'])

        # # pay loan fee
        # RepaymentFactory(amount=200, loan=loan)

        # # data = {
        # #     "provider_transaction_id": 31459,
        # #     "transfer": transfer.id,
        # # }
        # # this action require zw_disburse_loan permission
        # response = self.client.patch('/api/v1/disbursements/' + str(disbursement.id) + '/disburse/', format='json')
        # self.assertEqual(response.status_code, status.HTTP_200_OK)


def sms_parse(message):
    """
    helper function for checking reconciliation - to DRY SMSMessage create and parse_message
    this return WaveMoneyReceiveSMS object
    """
    sms = SMSMessage.objects.create(
        sender='Wave Money',
        sent_timestamp=timezone.now().timestamp() * 1000,  # sent_timestamp is unix milliseconds
        message=message
    )
    return sms.parse_message()


def generate_repayments(agent, amount, times):
    """
    helper function to create Repayment
    """
    return RepaymentFactory.create_batch(size=times, date=date.today(), loan__borrower__agent=agent, amount=amount)


class ReconciliationTests():
    """
    # FIXME: test cases here maybe redundant since loans.tasks.reonciliation is no longer needed
    Test reconciliations and its helper functions in tasks
    """

    # helper functions
    def reconciliation_status_check(self, repay_status, receive_status):
        """
        helper function for checking reconciliation
        check if input numbers of (NOT_RECONCILED,AUTO_RECONCILED,NEED_MANUAL_RECONCILIATION)
        match with count of (NOT_RECONCILED,AUTO_RECONCILED,NEED_MANUAL_RECONCILIATION) in database
        :param repay_status: tuple(NOT_RECONCILED,AUTO_RECONCILED,NEED_MANUAL_RECONCILIATION)
        :param receive_status: tuple(NOT_RECONCILED,AUTO_RECONCILED,NEED_MANUAL_RECONCILIATION)
        The tuple contains count of NOT_RECONCILED,AUTO_RECONCILED,NEED_MANUAL_RECONCILIATION
        :return: None
        """
        self.assertEqual(Repayment.objects.filter(reconciliation_status=NOT_RECONCILED).count(), repay_status[0])
        self.assertEqual(Repayment.objects.filter(reconciliation_status=AUTO_RECONCILED).count(), repay_status[1])
        self.assertEqual(Repayment.objects.filter(reconciliation_status=NEED_MANUAL_RECONCILIATION).count(),
                         repay_status[2])
        self.assertEqual(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NOT_RECONCILED).count(),
                         receive_status[0])
        self.assertEqual(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=AUTO_RECONCILED).count(),
                         receive_status[1])
        self.assertEqual(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NEED_MANUAL_RECONCILIATION).count(),
                         receive_status[2])

    # tests
    def test_repayments_by_sender_normal(self):
        """
        normal test case
        """
        # freeze_time need to use with tz_offset so that local time and UTC time are different
        # the time is chosen so that it is different days for UTC and local time
        with freeze_time('2017-3-14 18:00:00', tz_offset=+6.5):
            # create agents
            agent1 = AgentFactory(
                name='Shiina Mashiro',
                wave_money_number='09123456789'
            )
            agent2 = AgentFactory(
                name='Kanda Sorata',
                wave_money_number='09987654321'
            )
            agent3 = AgentFactory(
                name='Aoyama Nanami',
                wave_money_number='09111222333'
            )

            # create repayments for each agent
            repay1 = RepaymentFactory.create_batch(size=2, date=date.today(), loan__borrower__agent=agent1)
            repay2 = RepaymentFactory.create_batch(size=3, date=date.today(), loan__borrower__agent=agent2)
            repay3 = RepaymentFactory.create_batch(size=1, date=date.today(), loan__borrower__agent=agent3)

            # agents send money via Wave Money
            receive1 = sms_parse('You have received 4,000.00 Kyats from 9123456789.'
                                 ' Your new Wave Account balance is 584,600.00 Kyats.'
                                 ' Transaction ID 1112087818')
            receive2 = sms_parse('You have received 6,000.00 Kyats from 9987654321.'
                                 ' Your new Wave Account balance is 584,600.00 Kyats.'
                                 ' Transaction ID 1112087819')
            receive3 = sms_parse('You have received 2,000.00 Kyats from 9111222333.'
                                 ' Your new Wave Account balance is 584,600.00 Kyats.'
                                 ' Transaction ID 1112087810')

            # query list of repayments according to sender of wave money sms (probably agent)
            data1 = repayments_by_sender(receive1.sender)
            data2 = repayments_by_sender(receive2.sender)
            data3 = repayments_by_sender(receive3.sender)

            # check if queries above (data1,data2,data3) contain data that is supposed to contain
            self.assertEqual(len(data1), 2)
            # check if data1 contains the same primary keys as repay1
            self.assertSetEqual(set([d.pk for d in data1]), set([d.pk for d in repay1]))
            for d in data1:
                self.assertEqual(d.loan.borrower.agent.name, 'Shiina Mashiro')
                self.assertEqual(d.loan.borrower.agent.wave_money_number, '09123456789')
                self.assertEqual(d.reconciliation_status, NOT_RECONCILED)

            self.assertEqual(len(data2), 3)
            # check if data2 contains the same primary keys as repay2
            self.assertSetEqual(set([d.pk for d in data2]), set([d.pk for d in repay2]))
            for d in data2:
                self.assertEqual(d.loan.borrower.agent.name, 'Kanda Sorata')
                self.assertEqual(d.loan.borrower.agent.wave_money_number, '09987654321')
                self.assertEqual(d.reconciliation_status, NOT_RECONCILED)

            self.assertEqual(len(data3), 1)
            # check if data3 contains the same primary keys as repay3
            self.assertSetEqual(set([d.pk for d in data3]), set([d.pk for d in repay3]))
            for d in data3:
                self.assertEqual(d.loan.borrower.agent.name, 'Aoyama Nanami')
                self.assertEqual(d.loan.borrower.agent.wave_money_number, '09111222333')
                self.assertEqual(d.reconciliation_status, NOT_RECONCILED)

    def test_repayments_by_sender_3_cases(self):
        """
        NOT_RECONCILED for agent1
        AUTO_RECONCILED for agent2
        NEED_MANUAL_RECONCILIATION for agent3
        """
        with freeze_time('2017-3-14 18:00:00', tz_offset=+6.5):
            # agents created
            agent1 = AgentFactory(
                name='Shiina Mashiro',
                wave_money_number='09123456789'
            )
            agent2 = AgentFactory(
                name='Kanda Sorata',
                wave_money_number='09987654321'
            )
            agent3 = AgentFactory(
                name='Aoyama Nanami',
                wave_money_number='09111222333'
            )

            # repayments for each agent is created but reconciliation_status is different
            repay1 = RepaymentFactory.create_batch(size=3, date=date.today(), loan__borrower__agent=agent1)
            repay2 = RepaymentFactory.create_batch(size=2, date=date.today(), loan__borrower__agent=agent2,
                                                   reconciliation_status=AUTO_RECONCILED)
            repay3 = RepaymentFactory.create_batch(size=1, date=date.today(), loan__borrower__agent=agent3,
                                                   reconciliation_status=NEED_MANUAL_RECONCILIATION)

            # agents send money via wave money
            receive1 = sms_parse('You have received 4,000.00 Kyats from 9123456789.'
                                 ' Your new Wave Account balance is 584,600.00 Kyats.'
                                 ' Transaction ID 1112087818')

            # query repayments of sms sender (or) agent1
            data1 = repayments_by_sender(receive1.sender)
            # query repayments of agent2 where reconciliation_status is AUTO_RECONCILED
            data2 = repayments_by_sender('9987654321', status=AUTO_RECONCILED)
            # query repayments of agent3 where reconciliation_status is NEDD_MANUAL_RECONCILIATION
            data3 = repayments_by_sender('9111222333', status=NEED_MANUAL_RECONCILIATION)

            # check if queries above (data1,data2,data3) contain data that is supposed to contain
            self.assertEqual(len(data1), 3)
            # check if data1 contains the same primary keys as repay1
            self.assertSetEqual(set([d.pk for d in data1]), set([d.pk for d in repay1]))
            for d in data1:
                self.assertEqual(d.loan.borrower.agent.name, 'Shiina Mashiro')
                self.assertEqual(d.loan.borrower.agent.wave_money_number, '09123456789')
                self.assertEqual(d.reconciliation_status, NOT_RECONCILED)

            self.assertEqual(len(data2), 2)
            # check if data2 contains the same primary keys as repay2
            self.assertSetEqual(set([d.pk for d in data2]), set([d.pk for d in repay2]))
            for d in data2:
                self.assertEqual(d.loan.borrower.agent.name, 'Kanda Sorata')
                self.assertEqual(d.loan.borrower.agent.wave_money_number, '09987654321')
                self.assertEqual(d.reconciliation_status, AUTO_RECONCILED)

            self.assertEqual(len(data3), 1)
            # check if data3 contains the same primary keys as repay3
            self.assertSetEqual(set([d.pk for d in data3]), set([d.pk for d in repay3]))
            for d in data3:
                self.assertEqual(d.loan.borrower.agent.name, 'Aoyama Nanami')
                self.assertEqual(d.loan.borrower.agent.wave_money_number, '09111222333')
                self.assertEqual(d.reconciliation_status, NEED_MANUAL_RECONCILIATION)

    def test_repayments_by_sender_null(self):
        """
        Null case
        repayments_by_sender should return length 0 query when agent wave money number do not exist or status is invalid
        """
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5):
            # number do not exist
            data = repayments_by_sender('9111111111')
            self.assertEqual(len(data), 0)

            # empty string (no number)
            data = repayments_by_sender('')
            self.assertEqual(len(data), 0)

            # invalid number, invalid status
            data = repayments_by_sender('9111111111', status='RRRRR')
            self.assertEqual(len(data), 0)

    def test_repayments_by_sender_future(self):
        """
        Check if repayments_by_sender do not return repayments from future
        """
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5):
            agent = AgentFactory(
                wave_money_number='09123456789',
            )

            # repayment from future
            RepaymentFactory(date=date.today() + timedelta(days=1), loan__borrower__agent=agent)

            # repayments from future don't count
            self.assertEqual(len(repayments_by_sender(agent.wave_money_number)), 0)

        # now in future!
        with freeze_time('2017-3-15 23:00:00', tz_offset=+6.5):
            # the repayment is not in future anymore!
            self.assertEqual(len(repayments_by_sender(agent.wave_money_number)), 1)

    def test_reconciliation_future(self):
        """
        check if repayments and sms from future are not considered in reconciliation
        """
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5):
            agent = AgentFactory(
                wave_money_number='09123456789',
            )

            receive = sms_parse('You have received 4,000.00 Kyats from 9123456789.'
                                ' Your new Wave Account balance is 584,600.00 Kyats.'
                                ' Transaction ID 1112087818')
            # sms from future!
            receive.sent_at = timezone.now() + timedelta(days=1)
            receive.save()

            # repayment from future!
            repay = RepaymentFactory(date=date.today() + timedelta(days=1), loan__borrower__agent=agent, amount=4000)

            reconciliation()
            self.assertEqual(receive.reconciliation_status, NOT_RECONCILED)
            self.assertEqual(repay.reconciliation_status, NOT_RECONCILED)

        # just add a minute to be safe
        with freeze_time('2017-3-15 23:01:00', tz_offset=+6.5):
            # now in future and they are reconciled!
            reconciliation()
            receive.refresh_from_db()
            repay.refresh_from_db()
            self.assertEqual(receive.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(repay.reconciliation_status, AUTO_RECONCILED)

    def test_reconciliation_sms_set_manual(self):
        """
        set reconciliation_status = NEED_MANUAL_RECONCILIATION for sms from past
        """
        with freeze_time('2017-3-14 18:00:00', tz_offset=+6.5):
            # receive sms on today
            sender = '9123456789'
            sms_parse('You have received 4,000.00 Kyats from ' + sender + '.' +
                      ' Your new Wave Account balance is 584,600.00 Kyats.'
                      ' Transaction ID 1112087818')

        # receive sms yesterday
        with freeze_time('2017-3-13 18:00:00', tz_offset=+6.5):
            sms_parse('You have received 4,000.00 Kyats from ' + sender + '.' +
                      ' Your new Wave Account balance is 584,600.00 Kyats.'
                      ' Transaction ID 2112087818')

        # receive sms from future
        with freeze_time('2017-3-15 18:00:00', tz_offset=+6.5):
            sms_parse('You have received 6,000.00 Kyats from ' + sender + '.' +
                      ' Your new Wave Account balance is 84,600.00 Kyats.'
                      ' Transaction ID 2212087818')

        with freeze_time('2017-3-14 18:00:00', tz_offset=+6.5):
            # check if sms from past is set to NEED_MANUAL_RECONCILIATION
            self.assertEqual(len(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NOT_RECONCILED)), 3)
            self.assertEqual(len(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NEED_MANUAL_RECONCILIATION)), 0)
            reconciliation()
            self.assertEqual(len(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NOT_RECONCILED)), 2)
            self.assertEqual(len(WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NEED_MANUAL_RECONCILIATION)), 1)

    def test_reconciliation_repayment_set_manual(self):
        """
        set reconciliation_status = NEED_MANUAL_RECONCILIATION for repayments from past
        """
        with freeze_time('2017-3-13 18:00:00', tz_offset=+6.5):
            # repayments from yesterday
            RepaymentFactory.create_batch(size=2, date=date.today())

        with freeze_time('2017-3-14 18:00:00', tz_offset=+6.5):
            # repayments today
            RepaymentFactory.create_batch(size=2, date=date.today())

            # check if 2 of repayments(hopefully from yesterday) are set to NEED_MANUAL_RECONCILIATION
            self.assertEqual(len(Repayment.objects.filter(reconciliation_status=NOT_RECONCILED)), 4)
            self.assertEqual(len(Repayment.objects.filter(reconciliation_status=NEED_MANUAL_RECONCILIATION)), 0)
            reconciliation()
            self.assertEqual(len(Repayment.objects.filter(reconciliation_status=NOT_RECONCILED)), 2)
            self.assertEqual(len(Repayment.objects.filter(reconciliation_status=NEED_MANUAL_RECONCILIATION)), 2)

    def test_reconciliation_all_reconciled(self):
        """
        Testing reconciliation function: test case with everything AUTO_RECONCILED
        """
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5):
            # create agents
            agent1 = AgentFactory(
                wave_money_number='09123456789'
            )
            agent2 = AgentFactory(
                wave_money_number='09987654321'
            )
            agent3 = AgentFactory(
                wave_money_number='09111222333'
            )

            # generate repayments for agents
            generate_repayments(agent1, 2000, 2)
            generate_repayments(agent2, 3000, 3)
            generate_repayments(agent3, 5000, 1)

            # agents send money via wave money
            sms_parse('You have received 4,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 584,600.00 Kyats.'
                      ' Transaction ID 1112087818')
            sms_parse('You have received 9,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 594,600.00 Kyats.'
                      ' Transaction ID 1112087819')
            sms_parse('You have received 5,000.00 Kyats from 9111222333.'
                      ' Your new Wave Account balance is 614,600.00 Kyats.'
                      ' Transaction ID 1112087810')

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            self.reconciliation_status_check((6, 0, 0), (3, 0, 0))
            reconciliation()
            self.reconciliation_status_check((0, 6, 0), (0, 3, 0))

    def test_reconciliation_late_sms(self):
        """
        receive sms is late
        """
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5):
            # create agents
            agent1 = AgentFactory(
                wave_money_number='09123456789'
            )
            agent2 = AgentFactory(
                wave_money_number='09987654321'
            )

            # generate repayments for agents
            generate_repayments(agent1, 3000, 2)
            generate_repayments(agent1, 2000, 3)
            generate_repayments(agent2, 5000, 2)

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            # nth should be changed since nth reconciled
            self.reconciliation_status_check((7, 0, 0), (0, 0, 0))
            reconciliation()
            self.reconciliation_status_check((7, 0, 0), (0, 0, 0))

            # another repayment
            generate_repayments(agent1, 4000, 1)

            # agent2 send money via wave money
            # sms is late (repayments are first, sms come later. So, sms is late)
            sms_parse('You have received 10,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 30,800.00 Kyats.'
                      ' Transaction ID 2112098819')

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            # agent2 is reconciled. So, NOT_RECONCILED should decrease and AUTO_RECONCILED should increase
            self.reconciliation_status_check((8, 0, 0), (1, 0, 0))
            reconciliation()
            self.reconciliation_status_check((6, 2, 0), (0, 1, 0))

            # agent1 send money via wave money
            # sms is late (repayments are first, sms come later. So, sms is late)
            sms_parse('You have received 16,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 34,800.00 Kyats.'
                      ' Transaction ID 1112097818')

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            # both agent1 and agent2 are reconciled. So, there should be 0 in NOT_RECONCILED and AUTO_RECONCILED should increase
            self.reconciliation_status_check((6, 2, 0), (1, 1, 0))
            reconciliation()
            self.reconciliation_status_check((0, 8, 0), (0, 2, 0))

    def test_reconciliation_late_repayment(self):
        """
        repayment is late
        """
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5):
            # create agents
            agent1 = AgentFactory(
                wave_money_number='09123456789'
            )
            agent2 = AgentFactory(
                wave_money_number='09987654321'
            )

            # agents send money but repayments are not received yet
            sms_parse('You have received 10,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 44,800.00 Kyats.'
                      ' Transaction ID 1112097818')
            sms_parse('You have received 11,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 30,800.00 Kyats.'
                      ' Transaction ID 2112098819')

            # repayment from agent1 arrived but the amount is not match
            generate_repayments(agent1, 4000, 2)

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            # only sms are received. So, there should be no changes.
            self.reconciliation_status_check((2, 0, 0), (2, 0, 0))
            reconciliation()
            self.reconciliation_status_check((2, 0, 0), (2, 0, 0))

            # repayments come
            generate_repayments(agent1, 2000, 1)
            generate_repayments(agent2, 3000, 3)

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            # agent1 reconciled. There should be some number in AUTO_RECONCILED
            self.reconciliation_status_check((6, 0, 0), (2, 0, 0))
            reconciliation()
            self.reconciliation_status_check((3, 3, 0), (1, 1, 0))

            # another repayments from agent2
            generate_repayments(agent2, 1000, 2)

            # check if (count of) reconciliation_status of repayments and sms are correct before and after reconciliation
            # both agent1 and agent2 are reconciled. So, there should be 0 in NOT_RECONCILED and AUTO_RECONCILED should increase
            self.reconciliation_status_check((5, 3, 0), (1, 1, 0))
            reconciliation()
            self.reconciliation_status_check((0, 8, 0), (0, 2, 0))

    def test_reconciliation_complex(self):
        """
        complex test case
        """
        # new agent, everything ok
        with freeze_time('2017-3-11 23:00:00', tz_offset=+6.5, tick=True):
            agent1 = AgentFactory(
                wave_money_number='09123456789'
            )
            generate_repayments(agent1, 4000, 3)

            self.reconciliation_status_check((3, 0, 0), (0, 0, 0))
            reconciliation()
            self.reconciliation_status_check((3, 0, 0), (0, 0, 0))

            sms_parse('You have received 12,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 44,800.00 Kyats.'
                      ' Transaction ID 1112097818')

            # agent1 reconciled
            self.reconciliation_status_check((3, 0, 0), (1, 0, 0))
            reconciliation()
            self.reconciliation_status_check((0, 3, 0), (0, 1, 0))

        # new agent, agent1 did not send money
        with freeze_time('2017-3-12 23:00:00', tz_offset=+6.5, tick=True):
            # new day - since everything reconciled yesterday, nth should be changed at the start of day.
            self.reconciliation_status_check((0, 3, 0), (0, 1, 0))
            reconciliation()
            self.reconciliation_status_check((0, 3, 0), (0, 1, 0))

            # agent1 send repayments
            generate_repayments(agent1, 4000, 3)

            self.reconciliation_status_check((3, 3, 0), (0, 1, 0))
            reconciliation()
            self.reconciliation_status_check((3, 3, 0), (0, 1, 0))

            agent2 = AgentFactory(
                wave_money_number='09987654321'
            )

            # agent1 send another repayments
            generate_repayments(agent1, 2000, 2)
            # agent2 send repayments
            generate_repayments(agent2, 3000, 2)

            self.reconciliation_status_check((7, 3, 0), (0, 1, 0))
            reconciliation()
            self.reconciliation_status_check((7, 3, 0), (0, 1, 0))

            # receive money from agent2
            sms_parse('You have received 11,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 64,800.00 Kyats.'
                      ' Transaction ID 1112457818')

            # received money from agent2 but still need repayments
            self.reconciliation_status_check((7, 3, 0), (1, 1, 0))
            reconciliation()
            self.reconciliation_status_check((7, 3, 0), (1, 1, 0))

            # agent2 send another repayments
            generate_repayments(agent2, 5000, 1)

            # agent2 is reconciled. NOT_RECONCILED should decrease and AUTO_RECONCILED should increase
            # but agent1 is not reconciled. So, it should not be 0 in NOT_RECONCILED of repayment
            self.reconciliation_status_check((8, 3, 0), (1, 1, 0))
            reconciliation()
            self.reconciliation_status_check((5, 6, 0), (0, 2, 0))
            # day end. Notice agent1 did not send money. Only repayments.

        # new agent, agent 1 did not send full amount, agent 3 did not send repayments
        with freeze_time('2017-3-13 23:00:00', tz_offset=+6.5, tick=True):
            # Since agent1 did not send money yesterday, repayments of agent1 should become NEED_MANUAL_RECONCILIATION
            # So, NOT_RECONCILED should be 0 and NEED_MANUAL_RECONCILATION of repayment should increase
            self.reconciliation_status_check((5, 6, 0), (0, 2, 0))
            reconciliation()
            self.reconciliation_status_check((0, 6, 5), (0, 2, 0))

            agent3 = AgentFactory(
                wave_money_number='09111222333'
            )

            self.reconciliation_status_check((0, 6, 5), (0, 2, 0))
            reconciliation()
            self.reconciliation_status_check((0, 6, 5), (0, 2, 0))

            # agent1 and 2 send repayments (total of agent1 (now) = 16000)
            generate_repayments(agent1, 4000, 3)
            generate_repayments(agent1, 2000, 2)
            generate_repayments(agent2, 3000, 2)

            self.reconciliation_status_check((7, 6, 5), (0, 2, 0))
            reconciliation()
            self.reconciliation_status_check((7, 6, 5), (0, 2, 0))

            # agent3 send money (she do not repayments yet)
            sms_parse('You have received 5,000.00 Kyats from 9111222333.'
                      ' Your new Wave Account balance is 100,700.00 Kyats.'
                      ' Transaction ID 1932457818')

            # since nth is reconciled. Numbers should be the same
            self.reconciliation_status_check((7, 6, 5), (1, 2, 0))
            reconciliation()
            self.reconciliation_status_check((7, 6, 5), (1, 2, 0))

            # repayments from agent1 and 2 (total of agent1 (now) = 26000)
            generate_repayments(agent1, 5000, 2)
            generate_repayments(agent2, 5000, 1)

            self.reconciliation_status_check((10, 6, 5), (1, 2, 0))
            reconciliation()
            self.reconciliation_status_check((10, 6, 5), (1, 2, 0))

            # agent1 only send 10000 instead of 26000. Maybe she will send later.
            sms_parse('You have received 10,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 100,700.00 Kyats.'
                      ' Transaction ID 1942437818')

            self.reconciliation_status_check((10, 6, 5), (2, 2, 0))
            reconciliation()
            self.reconciliation_status_check((10, 6, 5), (2, 2, 0))

            sms_parse('You have received 11,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 110,700.00 Kyats.'
                      ' Transaction ID 1942434510')

            # agent2 reconciled. So, NOT_RECONCILED should decrease and AUTO_RECONCILED should increase
            self.reconciliation_status_check((10, 6, 5), (3, 2, 0))
            reconciliation()
            self.reconciliation_status_check((7, 9, 5), (2, 3, 0))

            # day end. Notice agent1 did not send full amount and agent3 only send money not repayments.

        # everything ok
        with freeze_time('2017-3-14 23:00:00', tz_offset=+6.5, tick=True):
            # NOT_RECONCILED should be 0 because this is the start of new day.
            # Every NOT_RECONCILED should be NEED_MANUAL_RECONCILIATION.
            # So, NOT_RECONCILED should be 0 and AUTO_RECONCILED should stay the same and NEED_MANUAL_RECONCILATION should increase
            self.reconciliation_status_check((7, 9, 5), (2, 3, 0))
            reconciliation()
            self.reconciliation_status_check((0, 9, 12), (0, 3, 2))

            generate_repayments(agent1, 4000, 3)
            generate_repayments(agent1, 2000, 2)
            generate_repayments(agent1, 5000, 2)
            generate_repayments(agent2, 3000, 2)
            generate_repayments(agent2, 5000, 1)
            generate_repayments(agent3, 2500, 2)

            self.reconciliation_status_check((12, 9, 12), (0, 3, 2))
            reconciliation()
            self.reconciliation_status_check((12, 9, 12), (0, 3, 2))

            sms_parse('You have received 11,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 110,700.00 Kyats.'
                      ' Transaction ID 1952434511')

            self.reconciliation_status_check((12, 9, 12), (1, 3, 2))
            reconciliation()
            self.reconciliation_status_check((9, 12, 12), (0, 4, 2))

            sms_parse('You have received 5,000.00 Kyats from 9111222333.'
                      ' Your new Wave Account balance is 111,700.00 Kyats.'
                      ' Transaction ID 1953434522')
            sms_parse('You have received 26,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 120,700.00 Kyats.'
                      ' Transaction ID 2952433500')

            self.reconciliation_status_check((9, 12, 12), (2, 4, 2))
            reconciliation()
            self.reconciliation_status_check((0, 21, 12), (0, 6, 2))

    def test_reconciliation_timezone_1(self):
        """
        timezone testing - different days in local time but same day in UTC
        every sms and repayment that do not reconcile until local midnight are
        set to NEED_MANUAL_RECONCILIATION
        But midnight in local time is not midnight in UTC
        """
        # tz_offset=+6.5 is 'Asia/Rangoon' timezone (UTC + 6:30)
        with freeze_time(str(timezone.now().date()) + ' 17:00:00', tz_offset=+6.5):
            # UTC: 20xx-xx-xx 17:00:00  Asia/Rangoon: 20xx-xx-xx 23:30:00    (notice local time is near midnight)
            self.assertEqual(str(timezone.now().time()), '17:00:00')  # UTC
            self.assertEqual(str(datetime.now().time()), '23:30:00')  # local time (Asia/Rangoon)

            agent1 = AgentFactory(
                wave_money_number='09123456789',
            )
            agent2 = AgentFactory(
                wave_money_number='09111222333',
            )
            repay1 = generate_repayments(agent1, 5000, 1)[0]
            repay2 = generate_repayments(agent2, 5000, 1)[0]
            # agent1 reconcile
            sms1 = sms_parse('You have received 5,000.00 Kyats from 9123456789.'
                             ' Your new Wave Account balance is 584,600.00 Kyats.'
                             ' Transaction ID 1112087818')
            # agent2 not reconcile
            sms2 = sms_parse('You have received 3,000.00 Kyats from 9111222333.'
                             ' Your new Wave Account balance is 587,600.00 Kyats.'
                             ' Transaction ID 1112086820')

            reconciliation()

            repay1.refresh_from_db()
            repay2.refresh_from_db()
            sms1.refresh_from_db()
            sms2.refresh_from_db()

            self.assertEqual(repay1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(sms1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(repay2.reconciliation_status, NOT_RECONCILED)
            self.assertEqual(sms2.reconciliation_status, NOT_RECONCILED)

        with freeze_time(str(timezone.now().date()) + ' 18:00:00', tz_offset=+6.5):
            # UTC; 20xx-xx-xx 18:00:00  Asia/Rangoon: 20xx-xx-(xx+1) 00:30:00    (notice local time is one day ahead of UTC)
            self.assertEqual(str(timezone.now().time()), '18:00:00')  # UTC
            self.assertEqual(str(datetime.now().time()), '00:30:00')  # local time (Asia/Rangoon)

            reconciliation()

            repay1.refresh_from_db()
            repay2.refresh_from_db()
            sms1.refresh_from_db()
            sms2.refresh_from_db()

            # after reconciliation, agent2' data should set to NEED_MANUAL_RECONCILIATION since it is after midnight
            self.assertEqual(repay1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(sms1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(repay2.reconciliation_status, NEED_MANUAL_RECONCILIATION)
            self.assertEqual(sms2.reconciliation_status, NEED_MANUAL_RECONCILIATION)

    def test_reconciliation_timezone_2(self):
        """
        timezone testing - same day in local time but different days in UTC
        every sms and repayment that do not reconcile until local midnight are
        set to NEED_MANUAL_RECONCILIATION
        But midnight in local time is not midnight in UTC
        """
        with freeze_time(str(timezone.now().date()) + ' 23:00:00', tz_offset=+6.5):
            # UTC: 20xx-xx-(xx-1) 23:00:00  Asia/Rangoon: 20xx-xx-xx 05:30:00    (notice UTC is near midnight and one day late compare to local)
            self.assertEqual(str(timezone.now().time()), '23:00:00')  # UTC
            self.assertEqual(str(datetime.now().time()), '05:30:00')  # local time (Asia/Rangoon)

            agent1 = AgentFactory(
                wave_money_number='09123456789',
            )
            agent2 = AgentFactory(
                wave_money_number='09111222333',
            )
            repay1 = generate_repayments(agent1, 5000, 1)[0]
            repay2 = generate_repayments(agent2, 5000, 1)[0]
            # agent1 reconcile
            sms1 = sms_parse('You have received 5,000.00 Kyats from 9123456789.'
                             ' Your new Wave Account balance is 584,600.00 Kyats.'
                             ' Transaction ID 2112087818')
            # agent2 not reconcile
            sms2 = sms_parse('You have received 3,000.00 Kyats from 9111222333.'
                             ' Your new Wave Account balance is 587,600.00 Kyats.'
                             ' Transaction ID 3112086820')

            reconciliation()

            repay1.refresh_from_db()
            repay2.refresh_from_db()
            sms1.refresh_from_db()
            sms2.refresh_from_db()

            self.assertEqual(repay1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(sms1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(repay2.reconciliation_status, NOT_RECONCILED)
            self.assertEqual(sms2.reconciliation_status, NOT_RECONCILED)

        with freeze_time(str(timezone.now().date() + timedelta(days=1)) + ' 01:00:00', tz_offset=+6.5):
            # UTC; 20xx-xx-xx 01:00:00  Asia/Rangoon: 20xx-xx-xx 07:30:00    (notice new day in UTC but same day in local)
            self.assertEqual(str(timezone.now().time()), '01:00:00')  # UTC
            self.assertEqual(str(datetime.now().time()), '07:30:00')  # local time (Asia/Rangoon)

            reconciliation()

            repay1.refresh_from_db()
            repay2.refresh_from_db()
            sms1.refresh_from_db()
            sms2.refresh_from_db()

            # after reconciliation, agent2' data still should be NOT_RECONCILED since it is the same day in local
            self.assertEqual(repay1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(sms1.reconciliation_status, AUTO_RECONCILED)
            self.assertEqual(repay2.reconciliation_status, NOT_RECONCILED)
            self.assertEqual(sms2.reconciliation_status, NOT_RECONCILED)

    def test_reconciliation_intermediary(self):
        """
        Test intermediary model Reconciliation (imported as 'Recon' to avoid confusion)
        """
        with freeze_time('2017-3-14 18:00:00', tz_offset=+6.5):
            # create agents
            agent1 = AgentFactory(
                wave_money_number='09123456789'
            )
            agent2 = AgentFactory(
                wave_money_number='09987654321'
            )
            agent3 = AgentFactory(
                wave_money_number='09111222333'
            )

            # generate repayments for agents
            generate_repayments(agent1, 2000, 2)
            generate_repayments(agent2, 3000, 3)
            generate_repayments(agent3, 5000, 1)

            # agents send money via wave money (notice that agent3 amount do not match)
            sms_parse('You have received 4,000.00 Kyats from 9123456789.'
                      ' Your new Wave Account balance is 584,600.00 Kyats.'
                      ' Transaction ID 1112087818')
            sms_parse('You have received 9,000.00 Kyats from 9987654321.'
                      ' Your new Wave Account balance is 594,600.00 Kyats.'
                      ' Transaction ID 1112087819')
            sms_parse('You have received 3,000.00 Kyats from 9111222333.'
                      ' Your new Wave Account balance is 614,600.00 Kyats.'
                      ' Transaction ID 1112087810')

            # reconciliation create Reconciliation(Recon) objects so number should increase
            self.assertEqual(len(Recon.objects.all()), 0)
            reconciliation()
            self.assertEqual(len(Recon.objects.all()), 2)

            # i1 is for agent1 and repayments and i2 for agent2 and repayments
            i1 = Recon.objects.all().order_by('pk')[0]
            i2 = Recon.objects.all().order_by('pk')[1]

            # check if reconciled by bot
            bot = User.objects.get(username='reconciliation_bot')
            self.assertEqual(i1.reconciled_by, bot)
            self.assertEqual(i2.reconciled_by, bot)

            # check if repayment and sms ForeignKeys are set to respective Reconciliation(Recon) (Notice Agent3 didn't match)
            for repay in Repayment.objects.filter(amount=2000):
                self.assertEqual(repay.reconciliation, i1)
            self.assertEqual(WaveMoneyReceiveSMS.objects.get(amount=4000).reconciliation, i1)
            for repay in Repayment.objects.filter(amount=3000):
                self.assertEqual(repay.reconciliation, i2)
            self.assertEqual(WaveMoneyReceiveSMS.objects.get(amount=9000).reconciliation, i2)
            for repay in Repayment.objects.filter(amount=5000):
                self.assertEqual(repay.reconciliation, None)
            self.assertEqual(WaveMoneyReceiveSMS.objects.get(amount=3000).reconciliation, None)


class Reconciliation2Tests(TestCase):
    """
    Test loans.signals.reconciliation which is triggered when new `SuperUsertoLenderPayment` is created
    """

    # helper functions
    def reconciliation_status_check(self, repay_status, pay_status):
        """
        helper function for checking reconciliation
        check if input numbers of (NOT_RECONCILED,AUTO_RECONCILED)
        match with count of (NOT_RECONCILED,AUTO_RECONCILED) in database
        :param repay_status: tuple(NOT_RECONCILED,AUTO_RECONCILED)
        :param pay_status: tuple(NOT_RECONCILED,AUTO_RECONCILED)
        The tuple contains count of NOT_RECONCILED,AUTO_RECONCILED
        :return: None
        """
        self.assertEqual(Repayment.objects.filter(reconciliation_status=NOT_RECONCILED).count(), repay_status[0])
        self.assertEqual(Repayment.objects.filter(reconciliation_status=AUTO_RECONCILED).count(), repay_status[1])
        self.assertEqual(SuperUsertoLenderPayment.objects.filter(reconciliation_status=NOT_RECONCILED).count(), pay_status[0])
        self.assertEqual(SuperUsertoLenderPayment.objects.filter(reconciliation_status=AUTO_RECONCILED).count(), pay_status[1])

    def test_1(self):
        """
        same day
        """
        with freeze_time('2017-3-14 14:00:00'):
            s1 = AgentFactory()
            s2 = AgentFactory()

            # total of Repayment for superuser1: 2000
            # total of Repayment for superuser2: 3000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=1000, loan__borrower__agent=s2)

            # total of Payment for superuser1: 2000 (match)
            # total of Payment for superuser2: 3000 (match)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=2000)
            SuperUsertoLenderPaymentFactory(super_user=s2, transfer__amount=3000)

            self.reconciliation_status_check((0, 5), (0, 2))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 14, 14, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 14, 14, 0, 0, tzinfo=timezone.utc))

    def test_2(self):
        """
        different days
        """
        with freeze_time('2017-3-14 14:00:00'):
            s1 = AgentFactory()
            s2 = AgentFactory()

        with freeze_time('2017-3-15 14:00:00'):
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=1000, loan__borrower__agent=s2)
            # no data is reconciled so far
            self.reconciliation_status_check((5, 0), (0, 0))

        with freeze_time('2017-3-16 14:00:00'):
            # only 2000 is sent. 3000 missing
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=2000)
            self.reconciliation_status_check((3, 2), (0, 1))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 16, 14, 0, 0, tzinfo=timezone.utc))

        with freeze_time('2017-3-17 14:00:00'):
            SuperUsertoLenderPaymentFactory(super_user=s2, transfer__amount=3000)
            self.reconciliation_status_check((0, 5), (0, 2))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 16, 14, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 17, 14, 0, 0, tzinfo=timezone.utc))

    def test_3(self):
        """
        not reconcile first and then reconcile
        FIXME: this test case maybe be identical to test_2
        """
        with freeze_time('2017-3-14 19:00:00'):
            s1 = AgentFactory()

        with freeze_time('2017-3-15 19:00:00'):
            # total of Repayment is 8000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=2000, loan__borrower__agent=s1)
            # SuperUser only send 5000
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=5000)
            # no data is reconciled so far
            self.reconciliation_status_check((5, 0), (1, 0))

        with freeze_time('2017-3-16 20:00:00'):
            # pay missing payment
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=3000)
            self.reconciliation_status_check((0, 5), (0, 2))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 16, 20, 0, 0, tzinfo=timezone.utc))

    def test_4(self):
        """
        reconcile twice
        """
        with freeze_time('2017-3-14 19:00:00'):
            s1 = AgentFactory()

        with freeze_time('2017-3-15 19:00:00'):
            # total of Repayment is 13000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=2000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s1)

            self.reconciliation_status_check((6, 0), (0, 0))

            # SuperUser send 13000 (match)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=13000)

            self.reconciliation_status_check((0, 6), (0, 1))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 15, 19, 0, 0, tzinfo=timezone.utc))

        with freeze_time('2017-3-16 20:00:00'):
            # total of Repayment is 25000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=2000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=6000, loan__borrower__agent=s1)

            self.reconciliation_status_check((8, 6), (0, 1))

            # SuperUser send 25000 (match)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=25000)

            self.reconciliation_status_check((0, 14), (0, 2))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 16, 20, 0, 0, tzinfo=timezone.utc))

    def test_5(self):
        """
        complex test case
        """
        # new SuperUser, everything ok
        with freeze_time('2017-3-11 23:00:00'):
            s1 = AgentFactory()
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=4000, loan__borrower__agent=s1)

            self.reconciliation_status_check((3, 0), (0, 0))

            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=12000)

            # superuser1 reconciled
            self.reconciliation_status_check((0, 3), (0, 1))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 11, 23, 0, 0, tzinfo=timezone.utc))

        # new SuperUser, superuser1 will not send money
        with freeze_time('2017-3-12 23:00:00'):
            # superuser1 send repayments
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=4000, loan__borrower__agent=s1)

            self.reconciliation_status_check((3, 3), (0, 1))

            s2 = AgentFactory()

            # superuser1 send more repayments
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=2000, loan__borrower__agent=s1)
            # superuser2 send repayments
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=3000, loan__borrower__agent=s2)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s2)

            self.reconciliation_status_check((8, 3), (0, 1))

            # receive money from superuser2
            SuperUsertoLenderPaymentFactory(super_user=s2, transfer__amount=11000)

            # superuser2 is reconciled. NOT_RECONCILED should decrease and AUTO_RECONCILED should increase
            # but superuser1 is not reconciled. So, it should not be 0 in NOT_RECONCILED of repayment
            self.reconciliation_status_check((5, 6), (0, 2))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 11, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 12, 23, 0, 0, tzinfo=timezone.utc))
            # day end. Notice superuser1 did not send money. Only repayments. (debt=16000)

        # superuser 1 will not send full amount
        with freeze_time('2017-3-13 23:00:00'):
            # superuser1 and 2 send repayments (total of superuser1 (now) = 26000(today) + 16000(yesterday) = 42000)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=4000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=2000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=5000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=3000, loan__borrower__agent=s2)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s2)

            self.reconciliation_status_check((15, 6), (0, 2))

            # superuser1 only send 10000 instead of 42000. Maybe it will be sent later.
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=10000)

            self.reconciliation_status_check((15, 6), (1, 2))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 11, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 12, 23, 0, 0, tzinfo=timezone.utc))

            SuperUsertoLenderPaymentFactory(super_user=s2, transfer__amount=11000)

            # superuser2 reconciled. So, NOT_RECONCILED should decrease and AUTO_RECONCILED should increase
            self.reconciliation_status_check((12, 9), (1, 3))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 11, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 13, 23, 0, 0, tzinfo=timezone.utc))

            # day end. Notice superuser1 did not send full amount (debt=32000)

        # new SuperUser, everything ok
        with freeze_time('2017-3-14 23:00:00'):
            s3 = AgentFactory()

            RepaymentFactory.create_batch(size=3, date=date.today(), amount=4000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=2000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=5000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=3000, loan__borrower__agent=s2)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s2)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=2500, loan__borrower__agent=s3)

            self.reconciliation_status_check((24, 9), (1, 3))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 11, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 13, 23, 0, 0, tzinfo=timezone.utc))
            # default magic number for new SuperUser
            self.assertEqual(s3.last_reconciled, datetime(1999, 1, 1, 1, 1, 1, tzinfo=timezone.utc))

            # superuser2 send money and reconcile
            SuperUsertoLenderPaymentFactory(super_user=s2, transfer__amount=11000)

            self.reconciliation_status_check((21, 12), (1, 4))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 11, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 14, 23, 0, 0, tzinfo=timezone.utc))
            # default magic number for new SuperUser
            self.assertEqual(s3.last_reconciled, datetime(1999, 1, 1, 1, 1, 1, tzinfo=timezone.utc))

            # superuser 1 pay missing repayments
            SuperUsertoLenderPaymentFactory(super_user=s3, transfer__amount=5000)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=58000)

            self.reconciliation_status_check((0, 33), (0, 7))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 14, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s2.last_reconciled, datetime(2017, 3, 14, 23, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(s3.last_reconciled, datetime(2017, 3, 14, 23, 0, 0, tzinfo=timezone.utc))

    def test_6(self):
        """
        missing payment
        sometimes sms are missing and add late via excel
        """
        with freeze_time('2017-3-15 02:30:00'):
            s1 = AgentFactory()
            # total of Repayment is 13000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=2000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s1)

            self.reconciliation_status_check((6, 0), (0, 0))

            # SuperUser send 13000 (match)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=13000)

            self.reconciliation_status_check((0, 6), (0, 1))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 15, 2, 30, 0, tzinfo=timezone.utc))

        with freeze_time('2017-3-16 02:30:00'):
            # total of Repayment is 25000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=3, date=date.today(), amount=2000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=5000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=6000, loan__borrower__agent=s1)

            # payment missing
            self.reconciliation_status_check((8, 6), (0, 1))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 15, 2, 30, 0, tzinfo=timezone.utc))

        with freeze_time('2017-3-17 02:30:00'):
            # total of Repayment is 23000
            RepaymentFactory.create_batch(size=1, date=date.today(), amount=7000, loan__borrower__agent=s1)
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=8000, loan__borrower__agent=s1)

            self.reconciliation_status_check((11, 6), (0, 1))

            # SuperUser send 23000 (match)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=23000)

            self.reconciliation_status_check((11, 6), (1, 1))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 15, 2, 30, 0, tzinfo=timezone.utc))

            # add missing payment from 16th (maybe via excel)
            SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=25000, transfer__timestamp=datetime(2017, 3, 16, 2, 30, 0, tzinfo=timezone.utc))

            # it is reconcile now
            self.reconciliation_status_check((0, 17), (0, 3))
            self.assertEqual(s1.last_reconciled, datetime(2017, 3, 17, 2, 30, 0, tzinfo=timezone.utc))

    def test_delay_success(self):
        """
        Transaction success is delay
        e.g. in pay with wave case, at first success is None. It take a while to get success
        """
        with freeze_time('2017-3-14 14:00:00'):
            s1 = AgentFactory()

            # total of Repayment for superuser1: 2000
            RepaymentFactory.create_batch(size=2, date=date.today(), amount=1000, loan__borrower__agent=s1)

            # total of Payment for superuser1: 2000 (match)
            su2l = SuperUsertoLenderPaymentFactory(super_user=s1, transfer__amount=2000, transfer__transfer_successful=None)

            # not reoncile yet
            self.reconciliation_status_check((2, 0), (1, 0))

            # delay success
            su2l.transfer.transfer_successful = True
            su2l.transfer.save()
            #TODO ==> Need to fix this test case
            # self.reconciliation_status_check((0, 2), (0, 1))
            # self.assertEqual(s1.last_reconciled, datetime(2017, 3, 14, 14, 0, 0, tzinfo=timezone.utc))


class ReconciliationAPITests(APITestCase):
    """
    Test api/v1/reconciliation-api/
    """
    @staticmethod
    def extract_ids_from_list(lst):
        lst = list(map(lambda x: x.id, lst))
        return lst

    def id_set(self, data):
        """
        convient function to extract set of ids for comparsion
        :param data: list of objects or queryset
        :return: set of ids
        """
        if type(data) is list:
            return set(self.extract_ids_from_list(data))
        # QuerySet
        else:
            return set(data.values_list('id', flat=True))

    def token_authenticate(self, user):
        """
        DRY steps for token authentication
        :param user: User object
        :return: None
        """
        if user.is_staff:
            pwd = '1234qwer'
        else:
            pwd = '198412'
        user.set_password(pwd)
        user.save()
        response = self.client.post('/api/v1/auth-token/', {'username': user.username, 'password': pwd})
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + response.data['token'])

    def test_1(self):
        """
        Test for permissions
        """
        # data
        superuser = AgentFactory()
        # unequal amount is intentionally used to prevent auto reconciliation
        repayments = RepaymentFactory.create_batch(size=3, date=date.today(), amount=1000, loan__borrower__agent=superuser)[:2]
        transactions = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser, transfer__amount=2000)[:1]
        staff = UserFactory(is_staff=True)

        # api call
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(transactions)}

        self.token_authenticate(user=superuser.user)
        response_superuser = self.client.post(url, data, format='json')

        self.token_authenticate(user=staff)
        response_staff = self.client.post(url, data, format='json')

        # check
        self.assertEqual(response_superuser.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotEqual(response_staff.status_code, status.HTTP_403_FORBIDDEN)

    def test_2(self):
        """
        Test for generic case
        """
        # data
        superuser = AgentFactory()
        # unequal amount is intentionally used to prevent auto reconciliation
        repayments = RepaymentFactory.create_batch(size=3, date=date.today(), amount=1000, loan__borrower__agent=superuser)[:2]
        su2lpayments = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser, transfer__amount=2000)[:1]
        staff = UserFactory(is_staff=True)

        # api call
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        recon = Reconciliation.objects.get(id=response.data['recon_id'])
        self.assertSetEqual(self.id_set(repayments), self.id_set(recon.repayment_list.all()))
        self.assertSetEqual(self.id_set(su2lpayments), self.id_set(recon.superusertolenderpayment_set.all()))
        self.assertEqual(recon.reconciled_by, staff)
        for d in repayments + su2lpayments:
            d.refresh_from_db()
            self.assertEqual(d.reconciliation_status, MANUAL_RECONCILED)

    def test_3(self):
        """
        Sent Repayments and SuperUsertoLenderPayment is linked to more than one SuperUser
        """
        # data
        superuser1 = AgentFactory()
        superuser2 = AgentFactory()
        # unequal amount is intentionally used to prevent auto reconciliation
        repayments1 = RepaymentFactory.create_batch(size=3, date=date.today(), amount=1000, loan__borrower__agent=superuser1)
        su2lpayments1 = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser1, transfer__amount=2000)
        repayments2 = RepaymentFactory.create_batch(size=2, date=date.today(), amount=2000, loan__borrower__agent=superuser2)
        su2lpayments2 = SuperUsertoLenderPaymentFactory.create_batch(size=1, super_user=superuser2, transfer__amount=3000)
        # the following data will be sent in api call
        # mixed repayments from both superuser1 and superuser2
        repayments = repayments1[0:1] + repayments2[0:1]
        su2lpayments = su2lpayments2[0:1]
        staff = UserFactory(is_staff=True)

        # api call
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check
        self.assertListEqual(response.data['error'], ['Those data are linked to more than one SuperUser'])

    def test_4(self):
        """
        Test for total not equal case
        """
        # data
        superuser = AgentFactory()
        # unequal amount is intentionally used to prevent auto reconciliation
        repayments = RepaymentFactory.create_batch(size=5, date=date.today(), amount=2000, loan__borrower__agent=superuser)[:3]
        su2lpayments = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser, transfer__amount=4000)[:2]
        staff = UserFactory(is_staff=True)

        # api call
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check
        self.assertListEqual(response.data['error'], ['Total of Repayment and total of SuperUsertoLenderPayment are not equal'])

    def test_5(self):
        """
        a little bit complex test case
        """
        # prepare data
        superuser1 = AgentFactory()
        superuser2 = AgentFactory()
        # unequal amount is intentionally used to prevent auto reconciliation
        repayments1 = RepaymentFactory.create_batch(size=11, date=date.today(), amount=1000, loan__borrower__agent=superuser1)
        su2lpayments1 = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser1, transfer__amount=5000)
        repayments2 = RepaymentFactory.create_batch(size=5, date=date.today(), amount=2000, loan__borrower__agent=superuser2)
        su2lpayments2 = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser2, transfer__amount=3000)
        staff = UserFactory(is_staff=True)

        # preprocess data for next api call
        repayments = repayments1[:5]
        su2lpayments = su2lpayments1[:1]

        # api call 1
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check if Reconciliation have correct data
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        recon = Reconciliation.objects.get(id=response.data['recon_id'])
        self.assertSetEqual(self.id_set(repayments), self.id_set(recon.repayment_list.all()))
        self.assertSetEqual(self.id_set(su2lpayments), self.id_set(recon.superusertolenderpayment_set.all()))
        self.assertEqual(recon.reconciled_by, staff)
        for d in repayments + su2lpayments:
            d.refresh_from_db()
            self.assertEqual(d.reconciliation_status, MANUAL_RECONCILED)

        # check if other data are not effected
        for d in repayments2 + su2lpayments2:
            d.refresh_from_db()
            self.assertEqual(d.reconciliation_status, NOT_RECONCILED)

        # preprocess data for next api call
        repayments = repayments2[:3]
        su2lpayments = su2lpayments2[:2]

        # api call 2
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check if Reconciliation have correct data
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        recon = Reconciliation.objects.get(id=response.data['recon_id'])
        self.assertSetEqual(self.id_set(repayments), self.id_set(recon.repayment_list.all()))
        self.assertSetEqual(self.id_set(su2lpayments), self.id_set(recon.superusertolenderpayment_set.all()))
        self.assertEqual(recon.reconciled_by, staff)
        for d in repayments + su2lpayments:
            d.refresh_from_db()
            self.assertEqual(d.reconciliation_status, MANUAL_RECONCILED)

        # preprocess data for next api call
        repayments = repayments1[5:10]
        su2lpayments = su2lpayments1[1:2]

        # api call 3
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check if Reconciliation have correct data
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        recon = Reconciliation.objects.get(id=response.data['recon_id'])
        self.assertSetEqual(self.id_set(repayments), self.id_set(recon.repayment_list.all()))
        self.assertSetEqual(self.id_set(su2lpayments), self.id_set(recon.superusertolenderpayment_set.all()))
        self.assertEqual(recon.reconciled_by, staff)
        for d in repayments + su2lpayments:
            d.refresh_from_db()
            self.assertEqual(d.reconciliation_status, MANUAL_RECONCILED)

    def test_6(self):
        """
        What happen what already reconciled data is sent?
        """
        # data

        superuser = AgentFactory()
        # unequal amount is intentionally used to prevent auto reconciliation
        all_repayments = RepaymentFactory.create_batch(size=10, date=date.today(), amount=1000, loan__borrower__agent=superuser)
        all_su2lpayments = SuperUsertoLenderPaymentFactory.create_batch(size=2, super_user=superuser, transfer__amount=2000)
        staff = UserFactory(is_staff=True)

        # select data for api call
        repayments = all_repayments[:2]
        su2lpayments = all_su2lpayments[:1]

        # first api call
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        response = self.client.post(url, data, format='json')

        # check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        recon = Reconciliation.objects.get(id=response.data['recon_id'])
        self.assertSetEqual(self.id_set(repayments), self.id_set(recon.repayment_list.all()))
        self.assertSetEqual(self.id_set(su2lpayments), self.id_set(recon.superusertolenderpayment_set.all()))
        self.assertEqual(recon.reconciled_by, staff)
        for d in repayments + su2lpayments:
            d.refresh_from_db()
            self.assertEqual(d.reconciliation_status, MANUAL_RECONCILED)

        # select data for api call
        repayments = all_repayments[3:5]
        su2lpayments = all_su2lpayments[:1]  # this is already reconciled data

        # api call should raise error
        url = reverse('recon')
        data = {'repayments': self.extract_ids_from_list(repayments), 'su2lpayments': self.extract_ids_from_list(su2lpayments)}
        self.token_authenticate(user=staff)
        self.assertRaises(TransitionNotAllowed, self.client.post, url, data, format='json')


class RepaymentFactoryTest(TestCase):
    """
    Test Repayment Factory with freezegun
    Freezegun is not working with factory boy
    """

    def test_freezegun(self):
        real_date = timezone.now().date()
        fake_date = timezone.now() - timedelta(days=3)

        freezer = freeze_time(fake_date, tick=True)
        freezer.start()

        repay = RepaymentFactory()
        # this give real date instead of fake date
        self.assertEqual(repay.date, real_date)

        # this give fake date correctly
        self.assertEqual(timezone.now().date(), fake_date)
        self.assertEqual(repay.recorded_at.date(), fake_date)

        freezer.stop()

    def test_freezegun_2(self):
        real_date = timezone.now().date()
        fake_date = timezone.now() - timedelta(days=3)

        with freeze_time(fake_date):
            repay = RepaymentFactory()
            # this give real date instead of fake date
            self.assertEqual(repay.date, real_date)

            # this give fake date correctly
            self.assertEqual(timezone.now().date(), fake_date)
            self.assertEqual(repay.recorded_at.date(), fake_date)


class SuperUsertoLenderPaymentAPITests(APITestCase):
    # TODO: add test with phone number start with 09
    def setUp(self):
        # mock Wave success response
        requests.post = MagicMock()
        mock_response = MagicMock()
        requests.post.return_value = mock_response
        mock_response.status_code = 200
        # FIXME: replace with variables instead of fixed values
        mock_response.json.return_value = {"purchaserMsisdn": '9111222333',
                                           "purchaserAmount": 7000,
                                           "statusCode": 102,
                                           "statusDescription": "Push notification sent"}

    def test_post(self):
        """
        General test for SuperUsertoLenderPaymentCustomView
        """
        s1 = AgentFactory(wave_money_number='09111222333')
        r1 = RepaymentFactory(loan__borrower__agent=s1, amount=5000)
        r2 = RepaymentFactory(loan__borrower__agent=s1, amount=2000)
        log_in_user = User.objects.get(username=s1.user)
        self.client.force_authenticate(user=log_in_user)
        data = {
            "amount": 7000,
            "method": 5,
            "details": {
                "sender_phone_number": "9111222333",
            },
            "repayments": [r1.pk, r2.pk]
        }
        response = self.client.post('/api/v1/pay/', data, format='json')

        # check response
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['amount'], 7000)

        # check if Transfer is created
        su2lender = SuperUsertoLenderPayment.objects.latest('id')
        transfer = Transfer.objects.latest('id')
        self.assertEqual(su2lender.transfer, transfer)

        # check if Transfer save response from Wave
        direct_response = {"purchaserMsisdn": '9111222333', "purchaserAmount": 7000, "statusCode": 102, "statusDescription": "Push notification sent"}
        self.assertEqual(su2lender.transfer.details['direct_response'], direct_response)

        # check if repayments link to this SuperusertoLenderPayment
        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertEqual(r1.superuser_to_lender_payment, su2lender)
        self.assertEqual(r2.superuser_to_lender_payment, su2lender)

    def test_unequal_amount(self):
        """
        test case for when money amount and sum of repayments are not equal
        """
        s1 = AgentFactory(wave_money_number='09111222333')
        r1 = RepaymentFactory(loan__borrower__agent=s1, amount=5000)
        r2 = RepaymentFactory(loan__borrower__agent=s1, amount=2000)
        log_in_user = User.objects.get(username=s1.user)
        self.client.force_authenticate(user=log_in_user)
        data = {
            "amount": 5000,
            "method": 5,
            "details": {
                "sender_phone_number": "9111222333",
            },
            "repayments": [r1.pk, r2.pk]
        }
        response = self.client.post('/api/v1/pay/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['success'], False)
        self.assertEqual(response.data['error'], 'money amount is not equal to sum of repayments')

    def test_sender_phone_number_missing(self):
        """
        test case for when sender_phone_number missing
        """
        s1 = AgentFactory(wave_money_number='09111222333')
        r1 = RepaymentFactory(loan__borrower__agent=s1, amount=5000)
        r2 = RepaymentFactory(loan__borrower__agent=s1, amount=2000)
        log_in_user = User.objects.get(username=s1.user)
        self.client.force_authenticate(user=log_in_user)
        data = {
            "amount": 7000,
            "method": 5,
            "details": {
            },
            "repayments": [r1.pk, r2.pk]
        }
        response = self.client.post('/api/v1/pay/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['success'], False)
        self.assertEqual(response.data['error'], 'sender_phone_number required')

    def test_invalid_method(self):
        """
        for now, this API only allow method 5 (Pay with Wave)
        """
        s1 = AgentFactory()
        r1 = RepaymentFactory(loan__borrower__agent=s1, amount=5000)
        r2 = RepaymentFactory(loan__borrower__agent=s1, amount=2000)
        log_in_user = User.objects.get(username=s1.user)
        self.client.force_authenticate(user=log_in_user)

        data = {
            "amount": 7000,
            "method": 4,
            "details": {
                "recipient_name": "Ma Khine",
                "recipient_phone_number": "09976864463",
                "recipient_NRC_number": "1/N/LaPaTha-123456"
            },
            "repayments": [r1.pk, r2.pk]
        }
        response = self.client.post('/api/v1/pay/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_501_NOT_IMPLEMENTED)
        self.assertEqual(response.data['success'], False)
        self.assertEqual(response.data['error'], 'transfer method not implemented')

    def test_superuser_only_view(self):
        """
        make sure only superuser can access this view
        """
        # send request as superuser
        su = AgentFactory()
        r1 = RepaymentFactory(loan__borrower__agent=su, amount=5000)
        r2 = RepaymentFactory(loan__borrower__agent=su, amount=2000)
        log_in_user = User.objects.get(username=su.user)
        self.client.force_authenticate(user=log_in_user)

        data = {
            "amount": 7000,
            "method": 5,
            "details": {
                "sender_phone_number": "9111222333",
            },
            "repayments": [r1.pk, r2.pk]
        }
        response = self.client.post('/api/v1/pay/', data, format='json')

        # check response
        self.assertEqual(response.data['success'], True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # send request as staff
        staff = UserFactory()
        self.client.force_authenticate(user=staff)

        response = self.client.post('/api/v1/pay/', data, format='json')

        # check response
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

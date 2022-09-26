from datetime import date, timedelta
import random
import factory
from borrowers.models import Agent, Borrower, Market, EducationLevel, BusinessType
from loans.models import Loan, Repayment, RepaymentScheduleLine, Disbursement, ACTUAL_360, ACTUAL_365, EQUAL_REPAYMENTS, \
    DISB_METHOD_WAVE_TRANSFER, DISB_METHOD_BANK_TRANSFER, DISB_METHOD_WAVE_N_CASH_OUT
from zw_utils.models import Currency
from django.contrib.auth.models import User
import math

# first, import a similar Provider or use the default one
from faker.providers import BaseProvider


class ZWProvider(BaseProvider):
    """
    A Faker provider to generate random burmese looking names
    """

    def burmese_name(self):
        name_bits = [
            'အောင်',
            'တင်',
            'သူ',
        ]
        name_length = random.randint(2, 3)
        return ''.join(random.sample(name_bits, name_length))


factory.Faker.add_provider(ZWProvider)


class CurrencyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Currency


class BusinessTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BusinessType


class EducationLevelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EducationLevel


class MarketFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Market


class AgentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Agent

    market = factory.SubFactory(MarketFactory)
    name = factory.Faker('name')


class BorrowerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Borrower

    date_joined = date.today()
    date_of_birth = date(1978, 10, 23)
    agent = factory.SubFactory(AgentFactory)
    name_mm = factory.Faker('burmese_name')
    name_en = factory.Faker('name')


class RepaymentScheduleLineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RepaymentScheduleLine


class LoanFactory(factory.django.DjangoModelFactory):
    """
    Create a loan object. Don't feed impossible cases to this factory, it will break!
    Note: the factory needs to be used within a freezegun context manager like:
    with freeze_time(date(2016, 10, 30)):
        loan = LoanFactory(...)
    """
    class Meta:
        model = Loan

    borrower = factory.SubFactory(BorrowerFactory)
    guarantor = factory.SubFactory(BorrowerFactory)
    loan_currency = factory.SubFactory(CurrencyFactory)
    loan_amount = 10000
    loan_fee = 500
    normal_repayment_amount = 1000  # some non zero values that won't cause an error
    bullet_repayment_amount = 1000

    @factory.post_generation
    def loans_lines(self, auto_create_lines=True, fee_on_disbursement=False, **kwargs):
        if auto_create_lines:
            # FIXME: no error handling for now, this is only used for testing!
            no_of_lines = int((self.loan_amount - self.bullet_repayment_amount) / self.normal_repayment_amount)
            # in EQUAL_REPAYMENTS, normal_repayment_amount = principal + interest
            if self.loan_interest_type == EQUAL_REPAYMENTS:
                no_of_lines = self.number_of_repayments

            for line_number in range(1, no_of_lines + 1):
                if line_number == 1:
                    fee = self.loan_fee * (not fee_on_disbursement)
                else:
                    fee = 0

                if self.loan_interest_type == ACTUAL_360 or self.loan_interest_type == ACTUAL_365:
                    # principal and interest will be take care by update_attributes_for_lines function below
                    self.lines.add(RepaymentScheduleLineFactory.create(
                        loan=self,
                        date=self.uploaded_at + timedelta(days=line_number),
                        principal=self.normal_repayment_amount,
                        fee=fee,
                    ))
                elif self.loan_interest_type == EQUAL_REPAYMENTS:
                    # principal and interest will be take care by update_attributes_for_lines function below
                    self.lines.add(RepaymentScheduleLineFactory.create(
                        loan=self,
                        date=self.uploaded_at + timedelta(days=line_number),
                        fee=fee,
                    ))

            if fee_on_disbursement:
                # add a line on the request upload day
                self.lines.add(RepaymentScheduleLineFactory.create(
                    loan=self,
                    date=self.uploaded_at,
                    fee=self.loan_fee
                ))

            # for now, do not consider bullet in EQUAL_REPAYMENTS
            if self.loan_interest_type != EQUAL_REPAYMENTS:
                self.lines.add(RepaymentScheduleLineFactory.create(
                    loan=self,
                    date=self.uploaded_at + timedelta(days=no_of_lines + 1),
                    principal=self.bullet_repayment_amount
                ))
            # set (principal and) interest for loan lines
            #self.update_attributes_for_lines()


class RepaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Repayment

    loan = factory.SubFactory(LoanFactory)
    date = date.today()
    amount = 2000
    fee = 0
    penalty = 0
    interest = 0
    principal = amount - fee


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    username = factory.Faker('name')
    email = factory.Faker('email')


class DisbursementFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Disbursement

    amount = 10000
    fees_paid = 100
    # disbursed_by = factory.SubFactory(UserFactory)
    disbursed_to = factory.SubFactory(AgentFactory)
    details = {}

    # @factory.post_generation
    # def details_for_method(self, dummy1, dummy2, **kwargs):
    #     """
    #     to assign corresponding details to method
    #     this is just to be safe and avoid possible bugs
    #     """
    #     if self.method == DISB_METHOD_KBZ_GENERIC:
    #         self.details = {
    #             "recipient_name": "Ma Khine",
    #             "recipient_phone_number": "09976864463",
    #             "recipient_NRC_number": "1/N/LaPaTha-123456"
    #         }
    #     elif self.method == DISB_METHOD_WAVE_P2P:
    #         self.details = {
    #             "recipient_number": "09795722081",
    #             "sender_number": "09795722082"
    #         }

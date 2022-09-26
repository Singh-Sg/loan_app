import graphene
from graphene.types.datetime import Date

from graphene_django.types import DjangoObjectType
from .decorators import permission_required

from .models import (BaseBorrowerSignature, Loan, PhotoSignature,
                     Repayment, RepaymentScheduleLine, LoanRequestReview)

from django.contrib.auth.models import User


class BaseBorrowerSignatureType(DjangoObjectType):
    api_result = graphene.String()
    photo = graphene.String()

    class Meta:
        model = BaseBorrowerSignature

    def resolve_api_result(self, info):
        """
        Get the api_result from the child PhotoSignature model
        """
        photosig = self.photosignature
        if photosig:
            return photosig.api_result
        return None

    def resolve_photo(self, info):
        """
        Get the photo url field from the child PhotoSignature model, to avoid nesting the PhotoSignature
        See https://stackoverflow.com/questions/49737118/django-multi-table-inheritance-and-graphene/49737563#49737563
        """
        photosig = self.photosignature
        if photosig:
            return photosig.photo
        return None


class PhotoSignatureType(DjangoObjectType):
    # TODO: see https://stackoverflow.com/questions/49737118/django-multi-table-inheritance-and-graphene
    # for a possible way to flatten the graphql result so that the model inheritance
    # works in python as well
    class Meta:
        model = PhotoSignature


class LoanType(DjangoObjectType):
    contract_due_date = Date(source='contract_due_date')

    class Meta:
        model = Loan


class RepaymentType(DjangoObjectType):
    class Meta:
        model = Repayment


class RepaymentScheduleLineType(DjangoObjectType):
    class Meta:
        model = RepaymentScheduleLine


class approveLoan(graphene.Mutation):

    id = graphene.Int()
    comments = graphene.String()
    loan = graphene.Int()
    approved = graphene.Boolean()
    alternativeOffer = graphene.Int()
    reviewer = graphene.Int()

    class Arguments:
        comments = graphene.String()
        loan = graphene.Int()
        alternativeOffer = graphene.Int()
        approved = graphene.Boolean()
        reviewer = graphene.Int()

    @permission_required('loans.zw_approve_loan')
    def mutate(self, info, comments, loan, reviewer, approved, alternativeOffer=None):
        loan = Loan.objects.get(id=loan)
        reviewer = User.objects.get(id=reviewer)
        if alternativeOffer:
            alternativeOffer = Loan.objects.get(id=alternativeOffer)
        loanRequestReview = LoanRequestReview(loan=loan, comments=comments, reviewer=reviewer,
                                              approved=approved,
                                              alternative_offer=alternativeOffer)
        loanRequestReview.save()
        return approveLoan(
            id=loanRequestReview.id,
            comments=loanRequestReview.comments,
            loan=loanRequestReview.loan,
            approved=loanRequestReview.loan,
        )


class Mutation(graphene.ObjectType):
    approveLoanReview = approveLoan.Field()


class Query(object):
    """
    Resolvers tell graphene how to handle various query formats
    """
    # query for a single loan by ID
    loan = graphene.Field(LoanType,
                          id=graphene.Int(),
                          )

    # query all loans (no filtering at all so far)
    all_loans = graphene.List(LoanType)

    # list all repayments/schedule lines
    all_repayments = graphene.List(RepaymentType)
    all_repaymentschedulelines = graphene.List(RepaymentScheduleLineType)

    def resolve_all_loans(self, info, **kwargs):
        return Loan.objects.all()

    def resolve_all_repayments(self, info, **kwargs):
        return Repayment.objects.select_related('loan').all()

    def resolve_loan(self, info, **kwargs):
        id = kwargs.get('id')
        if id is not None:
            return Loan.objects.get(pk=id)

        return None

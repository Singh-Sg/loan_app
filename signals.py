import simplejson as json
from datetime import date

import boto3
import logging
from django.contrib.auth.models import User
from django.db.models import Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from loans.models import Repayment, SuperUsertoLenderPayment, NOT_RECONCILED, AUTO_RECONCILED, DefaultPrediction, Loan
from loans.models import Reconciliation as Recon
from api_backend.settings import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY


@receiver(post_save, sender='loans.PhotoSignature')
def validate_signature(sender, instance=None, created=None, update_fields=None, **kwargs):
    print('validate_signature, created: ' + str(created))
    if kwargs['raw']:
        return
    if created:
        instance.validate()
        # loan state transition is called inside signal instead of save because
        # it seem photo thumbnails are only created after the instance is saved.
        # missing photo thumbnail will raise error in validate().
        instance.loan.sign_loan(signature=instance)
        instance.loan.save()
        instance.save()


def reconcile_with_intermediary(repayments, su2lenderpayments, reconciled_by=None, method=AUTO_RECONCILED):
    """
    connect `Repayment`s and `SuperUsertoLenderPayment`s via intermediary object
    intermediary object can contain additional info about reconciliation
    :param repayments: Queryset of Repayment
    :param su2lenderpayments: Queryset of SuperUsertoLenderPayment
    :param reconciled_by: User object
    :param method: AUTO_RECONCILED (or) MANUAL_RECONCILED
    :return: Reconciliation object
    """
    if reconciled_by is None:
        bot_user = User.objects.get(username='reconciliation_bot')
        intermediary = Recon.objects.create(reconciled_by=bot_user)
    else:
        intermediary = Recon.objects.create(reconciled_by=reconciled_by)
    for obj in repayments:
        obj.set_reconciled(intermediary, method)
    for obj in su2lenderpayments:
        obj.set_reconciled(intermediary, method)
    return intermediary


def reconciliation_by_superuser(superuser):
    # FIXME: reconciliation_status=NOT_RECONCILED maybe redundant
    # FIXME: maybe also filter with "less than instance.transfer.timestamp". In other words, future data of instance are discarded
    repayments = Repayment.objects.filter(loan__borrower__agent=superuser,
                                          recorded_at__gt=superuser.last_reconciled,
                                          reconciliation_status=NOT_RECONCILED)
    transactions = SuperUsertoLenderPayment.objects.filter(super_user=superuser,
                                                           transfer__timestamp__gt=superuser.last_reconciled,
                                                           reconciliation_status=NOT_RECONCILED,
                                                           transfer__transfer_successful=True)

    sum_repayments = repayments.aggregate(Sum('amount'))['amount__sum'] or 0
    sum_transactions = transactions.aggregate(Sum('transfer__amount'))['transfer__amount__sum'] or 0

    if sum_repayments == sum_transactions and sum_repayments != 0:
        reconcile_with_intermediary(repayments, transactions)
        superuser.last_reconciled = timezone.now()
        superuser.save()


@receiver(post_save, sender='loans.SuperUsertoLenderPayment')
def reconciliation(sender, instance=None, created=None, **kwargs):
    if hasattr(instance, 'transfer'):
        if created and instance.transfer.transfer_successful:
            reconciliation_by_superuser(instance.super_user)


@receiver(post_save, sender='loans.Loan')
def predict_default(sender, instance: Loan, created, *args, **kwargs):
    import sys
    if 'test' in sys.argv:
        return
    data = {
        'has_id_photo_front': instance.borrower.id_photo_front is not None,
        'has_id_photo_back': instance.borrower.id_photo_back is not None,
        'date_joined': (date.today() - instance.borrower.date_joined).seconds,
        'gender': instance.borrower.gender,
        'has_borrower_photo': instance.borrower.borrower_photo is not None,
        'has_business_address': instance.borrower.business_address is not None,
        'has_household_list_photo_back': instance.borrower.household_list_photo_back is not None,
        'has_household_list_photo_front': instance.borrower.household_list_photo_front is not None,
        'age': instance.borrower.age,
        'education_level_id': instance.borrower.education_level_id if instance.borrower.education_level_id is not None else 99,
        'num_of_people_in_hh': instance.borrower.num_of_people_in_hh,
        'years_at_current_location': instance.borrower.years_at_current_location,
        'business_expenses_high': instance.borrower.business_expenses_high,
        'household_expenses_high': instance.borrower.household_expenses_high,
        'household_expenses_low': instance.borrower.household_expenses_low,
        'business_expenses_low': instance.borrower.business_expenses_low,
        'agent_id': instance.borrower.agent_id,
        'has_fathers_name': instance.borrower.fathers_name is not None,
        'has_phone_number_ooredoo': instance.borrower.phone_number_ooredoo is not None or instance.borrower.phone_number_telenor is not None or instance.borrower.phone_number_mpt is not None,
        'reason_for_missing_nrc': instance.borrower.reason_for_missing_nrc if instance.borrower.reason_for_missing_nrc is not None else 0,
        'house_ownership': instance.borrower.house_ownership,
        'months_at_current_location': instance.borrower.months_at_current_location,
        'villagetract_id': instance.borrower.villagetract_id,
        'monthly_income_from_remittances': instance.borrower.monthly_income_from_remittances,
        'contract_date': (date.today() - instance.contract_date).seconds,
        'loan_amount': instance.loan_amount,
        'loan_interest_rate': instance.loan_interest_rate,
        'loan_fee': instance.loan_fee,
        'late_penalty_fee': instance.late_penalty_fee,
        'late_penalty_per_x_days': instance.late_penalty_per_x_days,
        'late_penalty_max_days': instance.late_penalty_max_days,
        'prepayment_penalty': instance.prepayment_penalty,
        'has_loan_contract_photo': instance.loan_contract_photo is not None,
        'effective_interest_rate': instance.effective_interest_rate if instance.effective_interest_rate is not None else 0,
        'number_of_repayments': instance.number_of_repayments,
        'bullet_repayment_amount': instance.bullet_repayment_amount,
        'normal_repayment_amount': instance.normal_repayment_amount
    }
    client = boto3.client('lambda', region_name='us-east-2', aws_access_key_id=AWS_ACCESS_KEY_ID,
         aws_secret_access_key=AWS_SECRET_ACCESS_KEY)
    try:
        response = client.invoke(FunctionName='zigway_credit_model_function', Payload=json.dumps(data), InvocationType='RequestResponse')
        prediction = json.loads(response['Payload'].read())
        DefaultPrediction.objects.get_or_create(loan=instance, passed_credit=prediction['will fail'])
    except Exception as e:
        logger = logging.getLogger('root')
        logger.error('Loan prediction error', exc_info=True, extra={
            'exception': e,
        })

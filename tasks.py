from __future__ import absolute_import, unicode_literals
# from celery import shared_task
from api_backend.celery_app import celery_app
from sms_gateway.models import WaveMoneyReceiveSMS
from loans.models import Repayment, NOT_RECONCILED, AUTO_RECONCILED, NEED_MANUAL_RECONCILIATION, Loan
from loans.models import Reconciliation as Recon  # to avoid confusion with reconciliation function
from datetime import datetime
from django.db.models import Sum
from django.utils import timezone
import logging
import pytz
from django.contrib.auth.models import User


# FIXME: all code below maybe redundant now because of reconciliation v2 in loans.signals
# @celery_app.on_after_finalize.connect
# def setup_periodic_tasks(sender, **kwargs):
#     sender.add_periodic_task(5.0, reconciliation.s())


def repayments_by_sender(sender, status=NOT_RECONCILED):
    """
    helper function for reconciliation
    return repayments where agent.wave_money_number = sender
    """
    local_date = timezone.localtime(timezone.now(), pytz.timezone('Asia/Rangoon')).date()
    return Repayment.objects.filter(
        reconciliation_status=status,
        date__lte=local_date,  # remove repayments from future
        loan__borrower__agent__wave_money_number=sender
    )


@celery_app.task(bind=True)
def reconciliation(args):
    """
    reconcile Wave Money Receive SMS and repayments
    - every sms and repayment that do not reconcile until local midnight are
      set to NEED_MANUAL_RECONCILIATION
    - every sms and repayment that reconcile (that mean money amount is equal)
      are set to AUTO_RECONCILED
    """
    try:
        # remove sms from future
        receive_sms = WaveMoneyReceiveSMS.objects.filter(reconciliation_status=NOT_RECONCILED, sent_at__lte=timezone.now())
        local_date = timezone.localtime(timezone.now(), pytz.timezone('Asia/Rangoon')).date()
        # set reconciliation_status = NEED_MANUAL_RECONCILIATION for
        # receive sms(NOT_RECONCILED) from past (local time zone)
        for wm_receive in receive_sms:
            # datetime are stored in UTC but we need to compare in local time
            wm_receive_local_date = timezone.localtime(wm_receive.sent_at, pytz.timezone('Asia/Rangoon')).date()
            if wm_receive_local_date < local_date:
                wm_receive.set_need_manual_reconciliation()
        receive_sms = receive_sms.exclude(reconciliation_status=NEED_MANUAL_RECONCILIATION)

        # set reconciliation_status = NEED_MANUAL_RECONCILIATION for repayment(NOT_RECONCILED)
        # that do not reconcile until local midnight
        for repay in Repayment.objects.filter(reconciliation_status=NOT_RECONCILED):
            if repay.date < local_date:
                repay.set_need_manual_reconciliation()

        # reconciliation
        # bot user to set for intermediary model
        bot_user = User.objects.get(username='reconciliation_bot')
        for wm_receive in receive_sms:
            repayment_set = repayments_by_sender(wm_receive.sender)
            if wm_receive.amount == repayment_set.aggregate(Sum('amount'))['amount__sum']:
                # intermediary model
                intermediary = Recon.objects.create(reconciled_by=bot_user)
                wm_receive.set_auto_reconciled(intermediary)
                for repay in repayment_set:
                    repay.set_auto_reconciled(intermediary)
    except Exception as e:
        logger = logging.getLogger('root')
        logger.error('reconciliation error', exc_info=True, extra={
            'error': e
        })


@celery_app.task(bind=True)
def update_attributes_for_loans_lines(args):
    """
    Just after midnight, call this function.
    adjust attributes for loan's RepaymentsScheduleLine
    """
    pass
    # FIXME: this requires interest calculation to be fixed to work
    # for ln in Loan.objects.all():
    #    if ln.repaid_on is not None:
    #        ln.update_attributes_for_lines()

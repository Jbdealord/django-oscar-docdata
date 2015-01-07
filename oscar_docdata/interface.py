"""
The main code to interface with Docdata.

This module is Oscar agnostic, and can be used in any other project.
The Oscar specific code is in the facade.
"""
from datetime import timedelta
import logging
from decimal import Decimal as D
from django.db import IntegrityError, transaction
from django.utils.timezone import now
from django.utils.translation import get_language
from oscar_docdata import appsettings
from oscar_docdata.gateway import DocdataClient
from oscar_docdata.models import DocdataOrder, DocdataPayment
from oscar_docdata.signals import order_status_changed, payment_added, payment_updated


logger = logging.getLogger(__name__)


class Interface(object):
    """
    The methods to interface with the Docdata gateway.
    """

    # TODO: is this really needed?
    status_mapping = {
        DocdataClient.STATUS_NEW: DocdataOrder.STATUS_NEW,
        DocdataClient.STATUS_STARTED: DocdataOrder.STATUS_NEW,
        DocdataClient.STATUS_REDIRECTED_FOR_AUTHENTICATION: DocdataOrder.STATUS_IN_PROGRESS,
        DocdataClient.STATUS_AUTHORIZATION_REQUESTED: DocdataOrder.STATUS_PENDING,
        DocdataClient.STATUS_AUTHORIZED: DocdataOrder.STATUS_PENDING,
        DocdataClient.STATUS_PAID: DocdataOrder.STATUS_PENDING,  # Overwritten when it's totals are checked.

        DocdataClient.STATUS_CANCELLED: DocdataOrder.STATUS_CANCELLED,
        DocdataClient.STATUS_CHARGED_BACK: DocdataOrder.STATUS_CHARGED_BACK,
        DocdataClient.STATUS_CONFIRMED_PAID: DocdataOrder.STATUS_PAID,
        DocdataClient.STATUS_CONFIRMED_CHARGEDBACK: DocdataOrder.STATUS_CHARGED_BACK,
        DocdataClient.STATUS_CLOSED_SUCCESS: DocdataOrder.STATUS_PAID,
        DocdataClient.STATUS_CLOSED_CANCELLED: DocdataOrder.STATUS_CANCELLED,
    }


    def __init__(self, testing_mode=None):
        """
        Initialize the interface.
        If the testing_mode is not set, it defaults to the ``DOCDATA_TESTING`` setting.
        """
        if testing_mode is None:
            testing_mode = appsettings.DOCDATA_TESTING
        self.testing_mode = testing_mode
        self.client = DocdataClient(testing_mode)


    def create_payment(self, order_number, total, user, language=None, description=None, profile=appsettings.DOCDATA_PROFILE, **kwargs):
        """
        Start a new payment session / container.

        This is the first step of any Docdata payment session.

        :param order_number: The order number generated by Oscar.
        :param total: The price object, including totals and currency.
        :type total: :class:`oscar.core.prices.Price`
        :type user: :class:`django.contrib.auth.models.User`
        :param language: The language to display the interface in.
        :param description
        :returns: The Docdata order reference ("order key").
        """
        if not language:
            language = get_language()

        # May raise an DocdataCreateError exception
        call_args = self.get_create_payment_args(
            # Pass all as kwargs, make it easier for subclasses to override using *args, **kwargs and fetch all by name.
            order_number=order_number,
            total=total,
            user=user,
            language=language,
            description=description,
            profile=profile,
            **kwargs
        )
        createsuccess = self.client.create(**call_args)

        # Track order_key for local logging
        destination = call_args.get('bill_to')
        self._store_create_success(
            order_number=order_number,
            order_key=createsuccess.order_key,
            amount=call_args['total_gross_amount'],
            language=language,
            country_code=destination.address.country_code if destination else None
        )

        # Return for further reference
        return createsuccess.order_key


    def get_create_payment_args(self, order_number, total, user, language=None, description=None, profile=appsettings.DOCDATA_PROFILE, **kwargs):
        """
        The arguments to pass to create a payment.
        This is implementation-specific, hence not implemented here.
        """
        raise NotImplementedError("Missing get_create_payment_args() implementation!")


    def _store_create_success(self, order_number, order_key, amount, language, country_code):
        """
        Store the order_key for local status checking.
        """
        DocdataOrder.objects.create(
            merchant_name=appsettings.DOCDATA_MERCHANT_NAME,
            merchant_order_id=order_number,
            order_key=order_key,
            total_gross_amount=amount.value,
            currency=amount.currency,
            language=language,
            country=country_code
        )


    def get_payment_menu_url(self, request, order_key, return_url=None, client_language=None, **extra_url_args):
        """
        Return the URL to the payment menu,
        where the user can be redirected to after creating a successful payment.

        For more information, see :func:`DocdataClient.get_payment_menu_url`.
        """
        return self.client.get_payment_menu_url(request, order_key, return_url=return_url, client_language=client_language, **extra_url_args)


    def start_payment(self, order_key, payment, payment_method=None):

        order = DocdataOrder.objects.select_for_update().current_merchant().get(order_key=order_key)
        amount = None

        # This can raise an exception.
        startsuccess = self.client.start(order_key, payment, payment_method=payment_method, amount=amount)

        self._set_status(order, DocdataOrder.STATUS_IN_PROGRESS)
        order.save()

        # Not updating the DocdataPayment objects here,
        # instead just wait for Docdata to call the status view.

        # Return for further reference.
        return startsuccess.payment_id


    def cancel_order(self, order):
        """
        Cancel the order.
        """
        client = DocdataClient()
        client.cancel(order.order_key)  # Can bail out with an exception (already logged)

        # Don't wait for server to send event back, get most recent state now.
        # Also make sure the order will be marked as cancelled.
        client = DocdataClient()
        statusreply = client.status(order.order_key)  # Can bail out with an exception (already logged)
        self._store_report(order, statusreply.report, indented_status=DocdataOrder.STATUS_CANCELLED)


    def update_order(self, order):
        """
        :type order: DocdataOrder
        """
        # Fetch the latest status
        client = DocdataClient()
        statusreply = client.status(order.order_key)  # Can bail out with an exception (already logged)

        # Store the new status
        self._store_report(order, statusreply.report)


    def _store_report(self, order, report, indented_status=None):
        """
        Store the retrieved status report in the order object.
        """
        # Store totals
        totals = report.approximateTotals
        order.total_registered = D(totals.totalRegistered) / 100
        order.total_shopper_pending = D(totals.totalShopperPending) / 100
        order.total_acquirer_pending = D(totals.totalAcquirerPending) / 100
        order.total_acquirer_approved = D(totals.totalAcquirerApproved) / 100
        order.total_captured = D(totals.totalCaptured) / 100
        order.total_refunded = D(totals.totalRefunded) / 100
        order.total_charged_back = D(totals.totalChargedback) / 100

        if hasattr(report, 'payment'):
            # Store all report lines, make an analytics of the new status
            latest_payment = self._store_report_lines(order, report)
            new_status = self._get_new_status(order, report, latest_payment)
        else:
            # There are no payments. It's really annoying to see that the Docdata status API
            # doesn't actually return a global "payment cluster" status code.
            # There are only status codes for the payment (which corresponds with a payment attempts by the user).
            # Make our best efforts here, based on some heuristics of the approximateTotals field.
            if totals.totalShopperPending == 0 \
            and totals.totalAcquirerPending == 0 \
            and totals.totalAcquirerApproved == 0 \
            and totals.totalCaptured == 0 \
            and totals.totalRefunded == 0 \
            and totals.totalChargedback == 0:
                # Everything is 0, either started, cancelled or expired
                if order.status == DocdataOrder.STATUS_CANCELLED:
                    new_status = order.status  # Stay in cancelled, don't become expired
                else:
                    if order.created < (now() - timedelta(days=21)):
                        # Will only expire old orders of more then 21 days.
                        new_status = indented_status or DocdataOrder.STATUS_EXPIRED
                    else:
                        # Either new or cancelled, can't determine!
                        new_status = indented_status or DocdataOrder.STATUS_NEW
            else:
                logger.error(
                    "Payment cluster {0} has no payment yet, and unknown 'approximateTotals' heuristics.\n"
                    "Status can't be reliably determined. Please investigate.\n"
                    "Totals={1}".format(order.order_key, totals)
                )
                if order.status in (DocdataOrder.STATUS_EXPIRED, DocdataOrder.STATUS_CANCELLED):
                    # Stay in cancelled/expired, don't switch back to NEW
                    new_status = order.status
                else:
                    new_status = indented_status or DocdataOrder.STATUS_NEW

        # Store status
        old_status = order.status
        status_changed = self._set_status(order, new_status)

        order.save()

        if status_changed:
            self.order_status_changed(order, old_status, order.status)


    def _set_status(self, order, new_status):
        """
        Changes the payment status to new_status and sends a signal about the change.
        """
        old_status = order.status
        if old_status != new_status:
            logger.info("Payment cluster {0} status changed {1} -> {2}".format(order.order_key, old_status, new_status))

            if new_status not in dict(DocdataOrder.STATUS_CHOICES):
                new_status = DocdataOrder.STATUS_UNKNOWN

            order.status = new_status
            return True
        else:
            return False


    def _store_report_lines(self, order, report):
        """
        Store the status report lines from the StatusReply.
        Each line represents a payment event.
        """
        latest_payment = None

        for payment_report in report.payment:
            # payment_report is a ns0:payment object, which contains:
            # - id            (paymentId, a positiveInteger)
            # - paymentMethod (string50)
            # - authorization  (authorization)
            #   - status      str
            #   - amount      (amount); value + currency attribute.
            #   - confidenceLevel  (string35)
            #   - capture     (capture); status, amount, reason
            #   - refund      (refund); status, amount, reason
            #   - chargeback  (chargeback); status, amount, reason
            # - extended      payment specific information, depends on payment method.

            # Find or create the correct payment object for current report.
            payment_class = DocdataPayment #TODO: self.id_to_model_mapping[order.payment_method_id]
            updated = False
            added = False

            try:
                ddpayment = payment_class.objects.select_for_update().get(payment_id=str(payment_report.id))
            except payment_class.DoesNotExist:
                # Create new line
                ddpayment = payment_class(
                    docdata_order=order,
                    payment_id=long(payment_report.id),
                    payment_method=str(payment_report.paymentMethod),
                )
                added = True

            if not payment_report.paymentMethod == ddpayment.payment_method:
                # Payment method change??
                logger.warn(
                    "Payment method from Docdata doesn't match saved payment method. "
                    "Storing the payment method received from Docdata for payment id {0}: {1}".format(
                        ddpayment.payment_id, payment_report.paymentMethod
                ))
                ddpayment.payment_method = str(payment_report.paymentMethod)
                updated = True

            # Store the totals
            authorization = payment_report.authorization
            old_values = (ddpayment.confidence_level, ddpayment.amount_allocated, ddpayment.amount_chargeback, ddpayment.amount_refunded, ddpayment.amount_debited)

            auth_status = str(authorization.status)
            ddpayment.confidence_level = authorization.confidenceLevel

            # Example XML of payment_report:
            #
            # <payment>
            #     <id>2481306128</id>
            #     <paymentMethod>IDEAL</paymentMethod>
            #     <authorization>
            #         <status>AUTHORIZED</status>
            #         <amount currency="EUR">7590</amount>
            #         <confidenceLevel>ACQUIRER_APPROVED</confidenceLevel>
            #         <capture>
            #             <status>CAPTURED</status>
            #             <amount currency="EUR">7590</amount>
            #         </capture>
            #     </authorization>
            # </payment>

            if auth_status == 'AUTHORIZED':
                # NOTE: currencies ignored here.
                # This only indicates the amount that's being dealt with.
                # the actual debited value is added when the value is captured.
                ddpayment.amount_allocated = _to_decimal(authorization.amount)

            if hasattr(authorization, 'capture'):
                # This means
                capture = authorization.capture[0]
                if capture.status == 'CAPTURED':
                    ddpayment.amount_debited = _to_decimal(capture.amount)
                else:
                    logger.debug("Capture of {0} is marked as {1}, not adding to totals".format(ddpayment.payment_id, capture.status))

            if hasattr(authorization, 'refund'):
                refund = authorization.refund[0]
                if refund.status == 'CAPTURED':
                    ddpayment.amount_refunded = _to_decimal(refund.amount)
                else:
                    logger.debug("Refund of {0} is marked as {1}, not adding to totals".format(ddpayment.payment_id, capture.status))

            if hasattr(authorization, 'chargeback'):
                chargeback = authorization.chargeback[0]
                if chargeback.status == 'CAPTURED':
                    ddpayment.amount_chargeback = _to_decimal(chargeback.amount)
                else:
                    logger.debug("Chargeback of {0} is marked as {1}, not adding to totals".format(ddpayment.payment_id, capture.status))

            # Track changes
            new_values = (ddpayment.confidence_level, ddpayment.amount_allocated, ddpayment.amount_chargeback, ddpayment.amount_refunded, ddpayment.amount_debited)
            if old_values != new_values:
                updated = True

            # Detect status change

            if ddpayment.status != auth_status:
                # Status change!
                logger.info("Docdata payment status changed. payment={0} status: {1} -> {2}".format(
                    payment_report.id, ddpayment.status, auth_status
                ))

                if auth_status not in DocdataClient.DOCUMENTED_STATUS_VALUES and auth_status not in DocdataClient.SEEN_UNDOCUMENTED_STATUS_VALUES:
                    # Note: We continue to process the payment status change on this error.
                    logger.warn("Received unknown payment status from Docdata. payment={0}, status={1}".format(
                        payment_report.id, auth_status
                    ))

                ddpayment.status = auth_status
                updated = True

            if added or updated:
                # Saving might happen concurrently, as the user returns to the OrderReturnView
                # and Docdata calls the StatusChangedNotificationView at the same time.
                sid = transaction.savepoint()  # for PostgreSQL
                try:
                    ddpayment.save()
                    transaction.savepoint_commit(sid)
                except IntegrityError:
                    transaction.savepoint_rollback(sid)
                    logger.warn("Experienced concurrency issues with update-status, payment id {0}: {1}".format(payment_report.id))

                    # Overwrite existing object instead.
                    #not needed, no impact on save: ddpayment._state.adding = False
                    ddpayment.id = str(payment_report.id)
                    ddpayment.save()
                    added = False

                # Fire events so payment transactions can be created in Oscar.
                # This can be used to call source.transactions.create(..) for example.
                if added:
                    payment_added.send(sender=DocdataPayment, order=order, payment=ddpayment)
                else:
                    payment_updated.send(sender=DocdataPayment, order=order, payment=ddpayment)

            # Webservice doesn't return payments in the correct order (or reversed).
            # So far, the payments can only be sorted by ID.
            if latest_payment is None or latest_payment.id < payment_report.id:
                latest_payment = payment_report


        # Use the latest payment to get a new status for the order.
        return latest_payment


    def _get_new_status(self, order, report, latest_payment_report):
        """
        Perform any checks related to the status change.
        This returns the "status" value of the last payment line.
        This line either indicates the payment is authorized, cancelled, refunded, etc..
        """
        status = str(latest_payment_report.authorization.status)
        totals = report.approximateTotals

        # Some status mapping overrides.
        # Using status of last payment report line.
        new_status = self.status_mapping.get(str(latest_payment_report.authorization.status), DocdataOrder.STATUS_UNKNOWN)

        # Stay in cancelled/expired, don't switch back to NEW
        # Even though the payment cluster is set to 'closed_expired',
        # Docdata doesn't expire the individual payment report lines.
        if order.status in (DocdataOrder.STATUS_EXPIRED, DocdataOrder.STATUS_CANCELLED) \
        and new_status in (DocdataOrder.STATUS_NEW, DocdataOrder.STATUS_IN_PROGRESS):
            new_status = order.status

        # Integration Manual Order API 1.0 - Document version 1.0, 08-12-2012 - Page 33:
        #
        # Safe route: The safest route to check whether all payments were made is for the merchants
        # to refer to the "Total captured" amount to see whether this equals the "Total registered
        # amount". While this may be the safest indicator, the downside is that it can sometimes take a
        # long time for acquirers or shoppers to actually have the money transferred and it can be
        # captured.
        #
        if status == DocdataClient.STATUS_AUTHORIZED:

            # Because currency conversions may cause payments to happen with a few cents less,
            # this workaround makes sure those orders will still be marked as paid!
            # If you don't like this, the alternative is using DOCDATA_PAYMENT_SUCCESS_MARGIN = {}
            # and listening for the callback=SUCCESS value in the `return_view_called` signal.
            margin = 0
            if order.currency == totals._exchangedTo:  # Reads XML attribute.
                if any(p.authorization.amount._currency != order.currency for p in report.payment):
                    margin = appsettings.DOCDATA_PAYMENT_SUCCESS_MARGIN.get(totals._exchangedTo, 0)
                    if margin >= totals.totalRegistered:  # avoid making everything as paid!
                        margin = 0


            if totals.totalCaptured >= (totals.totalRegistered - margin):
                payment_sum = (totals.totalCaptured - totals.totalChargedback - totals.totalRefunded)

                if payment_sum >= (totals.totalRegistered - margin):
                    # With all capture changes etc.. it's still what was registered.
                    # Full amount is paid.
                    new_status = DocdataOrder.STATUS_PAID
                    logger.info("Payment cluster {0} Registered: {1} >= Total Captured: {2} (margin: {3}); new status PAID".format(order.order_key, totals.totalRegistered, totals.totalCaptured, margin))

                elif payment_sum == 0:
                    # A payment was captured, but the totals are 0.
                    # See if there is a charge back or refund.
                    logger.info("Payment cluster {0} Total Registered: {1} Total Captured: {2} Total Chargedback: {3} Total Refunded: {4}".format(
                        order.order_key, totals.totalRegistered, totals.totalCaptured, totals.totalChargedback, totals.totalRefunded
                    ))

                    # See what happened with the last payment addition
                    authorization = latest_payment_report.authorization

                    # Chargeback.
                    # TODO: Add chargeback fee somehow (currently E0.50).
                    if totals.totalCaptured == totals.totalChargedback:
                        if hasattr(authorization, 'chargeback') and len(authorization.chargeback) > 0:
                            logger.info("Payment cluster {0} chargedback: {1}".format(order.order_key, authorization.chargeback[0].reason))
                        else:
                            logger.info("Payment cluster {0} chargedback.".format(order.order_key))

                        new_status = DocdataOrder.STATUS_CHARGED_BACK

                    # Refund.
                    # TODO: Log more info from refund when we have an example.
                    if totals.totalCaptured == totals.totalRefunded:
                        logger.info("Payment cluster {0} refunded.".format(order.order_key))
                        new_status = DocdataOrder.STATUS_REFUNDED

                    #payment.amount = 0
                    #payment.save()

                else:
                    # Show as error instead.
                    logger.error(
                        "Payment cluster {0} chargeback and refunded sum is negative. Please investigate.\n"
                        "Totals={1}".format(order.order_key, totals)
                    )
                    new_status = DocdataOrder.STATUS_UNKNOWN


        # Detect a nasty error condition that needs to be manually fixed.
        total_registered = long(totals.totalRegistered)
        total_gross_cents = long(order.total_gross_amount * 100)
        if new_status != DocdataOrder.STATUS_CANCELLED and total_registered != total_gross_cents:
            logger.error("Payment cluster {0} total: {1} does not equal Total Registered: {2}.".format(order.order_key, total_gross_cents, total_registered))

        return new_status

        # TODO Use status change log to investigate if these overrides are needed.
        # # These overrides are really just guessing.
        # latest_capture = authorization.capture[-1]
        # if status == 'AUTHORIZED':
        #     if hasattr(authorization, 'refund') or hasattr(authorization, 'chargeback'):
        #         new_status = 'cancelled'
        #     if latest_capture.status == 'FAILED' or latest_capture == 'ERROR':
        #         new_status = 'failed'
        #     elif latest_capture.status == 'CANCELLED':
        #         new_status = 'cancelled'


    def order_status_changed(self, docdataorder, old_status, new_status):
        """
        Notify that the order status changed.
        This function can be extended by inheriting the Facade class.
        """
        if old_status == new_status:
            return

        # Note that using a custom Facade class in your project doesn't help much,
        # as the Facade is also used by the default views.
        order_status_changed.send(sender=DocdataOrder, order=docdataorder, old_status=old_status, new_status=new_status)


def _to_decimal(amount):
    # Convert XML amount to decimal
    return D(long(amount.value)) / 100

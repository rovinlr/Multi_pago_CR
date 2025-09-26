# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

class BatchPaymentAllocationWizard(models.TransientModel):
    _name = "batch.payment.allocation.wizard"
    _description = "Batch Payment Allocation (One payment -> Many invoices)"

    partner_type = fields.Selection([("customer","Customer"),("supplier","Vendor")], required=True, default="supplier")
    partner_id = fields.Many2one(
        "res.partner",
        string="Partner",
        required=True,
        domain="['|', ('parent_id', '=', False), ('is_company', '=', False)]",
    )
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True, readonly=True)
    journal_id = fields.Many2one("account.journal", string="Payment Journal", required=True, domain="[('type','in',('bank','cash'))]")
    payment_method_line_id = fields.Many2one("account.payment.method.line", string="Payment Method", domain="[('journal_id','=',journal_id)]")
    payment_date = fields.Date(default=fields.Date.context_today, required=True)
    payment_currency_id = fields.Many2one("res.currency", string="Payment Currency", required=True, default=lambda self: self.env.company.currency_id)
    communication = fields.Char(string="Memo / Reference")

    allocation_mode = fields.Selection([("grouped", "One Grouped Payment"), ("per_invoice", "One Payment per Invoice")],
                                       default="grouped", required=True, string="Allocation Mode")
    rate_source = fields.Selection([("company", "Company Rates (res.currency.rate)"), ("custom", "Custom Rate")],
                                   default="company", required=True, string="FX Rate Source")
    custom_rate = fields.Float(string="Custom Rate (1 Company CCY -> Payment CCY)", digits=(16, 6))

    total_to_pay = fields.Monetary(string="Total to Pay", currency_field="payment_currency_id",
                                   compute="_compute_total_to_pay", store=False)
    line_ids = fields.One2many("batch.payment.allocation.wizard.line", "wizard_id", string="Invoices")

    # ---------- helpers ----------
    def _get_payment_currency(self):
        self.ensure_one()
        return self.journal_id.currency_id or self.company_id.currency_id

    def _validate_custom_rate(self):
        self.ensure_one()
        if self.rate_source == "custom":
            if not self.custom_rate:
                raise ValidationError(_("Please enter a positive Custom Rate."))
            if self.custom_rate <= 0:
                raise ValidationError(_("The Custom Rate must be strictly positive."))

    def _pay_to_company(self, amount_paycur, date):
        """Convert amount from payment/journal currency -> company currency."""
        self.ensure_one()
        pay_currency = self._get_payment_currency()
        company_currency = self.company_id.currency_id
        date = date or self.payment_date or fields.Date.context_today(self)
        amount_paycur = amount_paycur or 0.0
        if self.rate_source == "custom":
            rate = self.custom_rate or 0.0
            if not rate:
                return 0.0
            return company_currency.round(amount_paycur / rate)
        return pay_currency._convert(amount_paycur, company_currency, self.company_id, date)

    def _convert_amount(self, amount_company_ccy, date):
        """Convert from company currency -> payment/journal currency."""
        self.ensure_one()
        if not amount_company_ccy:
            return 0.0
        pay_currency = self._get_payment_currency()
        date = date or self.payment_date or fields.Date.context_today(self)
        if self.rate_source == "custom":
            rate = self.custom_rate or 0.0
            if not rate:
                return 0.0
            return pay_currency.round(amount_company_ccy * rate)
        return self.company_id.currency_id._convert(amount_company_ccy, pay_currency, self.company_id, date)

    # ---------- onchange ----------
    @api.onchange("journal_id")
    def _onchange_journal(self):
        for w in self:
            if not w.journal_id:
                continue
            w.payment_currency_id = w.journal_id.currency_id or w.company_id.currency_id
            methods = (w.journal_id.inbound_payment_method_line_ids if w.partner_type == "customer"
                       else w.journal_id.outbound_payment_method_line_ids)
            if not w.payment_method_line_id or (w.payment_method_line_id.journal_id != w.journal_id):
                w.payment_method_line_id = methods[:1].id if methods else False
            w._load_invoices()

    @api.onchange("partner_type", "partner_id", "payment_date")
    def _onchange_partner(self):
        for w in self:
            w._load_invoices()

    # ---------- load invoices ----------
    def _load_invoices(self):
        self.ensure_one()
        self.line_ids = [(5, 0, 0)]
        if not (self.partner_type and self.partner_id):
            return
        in_types = ("out_invoice","out_refund") if self.partner_type == "customer" else ("in_invoice","in_refund")
        moves = self.env["account.move"].search([
            ("move_type", "in", in_types),
            ("partner_id", "=", self.partner_id.id),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
            ("company_id", "=", self.company_id.id),
        ], order="invoice_date asc, name asc")

        lines = []
        for mv in moves:
            # Use receivable/payable lines to compute residuals
            rec_lines = mv.line_ids.filtered(lambda l: l.account_id and l.account_id.account_type in ('asset_receivable','liability_payable'))
            residual_company = abs(sum(rec_lines.mapped('amount_residual')))
            residual_invoice = abs(sum(rec_lines.mapped('amount_residual_currency'))) if mv.currency_id else residual_company
            if residual_company <= 0 and residual_invoice <= 0:
                continue
            residual_pay_cur = self._convert_amount(residual_company, self.payment_date)
            lines.append((0, 0, {
                "move_id": mv.id,
                "name": mv.name,
                "invoice_date": mv.invoice_date,
                "residual_in_company_currency": residual_company,
                "residual_in_invoice_currency": residual_invoice,
                "residual_in_payment_currency": residual_pay_cur,
                "amount_to_pay": residual_pay_cur,
            }))
        self.line_ids = lines

    # ---------- compute ----------
    @api.depends("line_ids.amount_to_pay")
    def _compute_total_to_pay(self):
        for w in self:
            w.total_to_pay = sum(w.line_ids.mapped("amount_to_pay"))

    # ---------- actions ----------
    def action_allocate(self):
        self.ensure_one()
        self._validate_custom_rate()
        if not self.line_ids:
            raise UserError(_("There are no invoice lines to pay."))
        if not self.journal_id:
            raise UserError(_("Please select a Payment Journal."))

        if not self.payment_method_line_id:
            method = (self.journal_id.inbound_payment_method_line_ids if self.partner_type == "customer"
                      else self.journal_id.outbound_payment_method_line_ids)[:1]
            if not method:
                raise UserError(_("The selected journal has no compatible payment method."))
            self.payment_method_line_id = method.id

        pay_currency = self._get_payment_currency()
        date = self.payment_date or fields.Date.context_today(self)

        chosen = self.line_ids.filtered(lambda l: l.amount_to_pay and l.amount_to_pay > 0.0)
        if not chosen:
            raise UserError(_("Please set a positive Amount to Pay for at least one invoice."))

        def _clamp_to_residual_paycur(line, amt_in_pay_currency):
            # Compare the user-entered amount (in payment/journal currency) with the residual in that same currency.
            rec_lines = line.move_id.line_ids.filtered(lambda l: l.account_id and l.account_id.account_type in ('asset_receivable','liability_payable'))
            pay_currency = self._get_payment_currency()
            invoice_currency = line.move_id.currency_id

            # Compute residual in payment currency
            if invoice_currency and invoice_currency == pay_currency:
                # Perfect: use residual in invoice currency directly to avoid FX drift
                residual_paycur = abs(sum(rec_lines.mapped('amount_residual_currency')))
            else:
                # Convert company residual to payment currency at the payment date
                residual_company = abs(sum(rec_lines.mapped('amount_residual')))
                residual_paycur = self._convert_amount(residual_company, date)

            amt_paycur = amt_in_pay_currency or 0.0
            if float_compare(amt_paycur, residual_paycur, precision_rounding=pay_currency.rounding) > 0:
                amt_paycur = residual_paycur
            if float_compare(amt_paycur, 0.0, precision_rounding=pay_currency.rounding) < 0:
                amt_paycur = 0.0
            return amt_paycur, residual_paycur

        # If grouped but mixed currencies, fallback to per-invoice
        mismatch = any(l.move_id.currency_id and l.move_id.currency_id != pay_currency for l in chosen)
        if self.allocation_mode == "grouped" and mismatch:
            self.allocation_mode = "per_invoice"

        if self.allocation_mode == "per_invoice":
            payment_ids = []
            for line in chosen:
                amt_paycur, _res = _clamp_to_residual_paycur(line, line.amount_to_pay or 0.0)
                amt_company = self._pay_to_company(amt_paycur, date)

                reg = self.env["account.payment.register"].with_context(
                    active_model="account.move", active_ids=[line.move_id.id]
                ).create({
                    "payment_date": date,
                    "journal_id": self.journal_id.id,
                    "payment_method_line_id": self.payment_method_line_id.id,
                    "currency_id": pay_currency.id,  # display currency
                    "amount": amt_company,          # amount is in company currency (Odoo 19)
                    "group_payment": False,
                    "communication": self.communication or "",
                })
                payments = reg._create_payments()
                if not payments:
                    raise UserError(_("No payments were created. Check the amounts to pay."))
                payment_ids += payments.ids

            if not payment_ids:
                raise UserError(_("No payments were created. Check the amounts to pay."))

            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.payment',
                'view_mode': 'list,form',
                'views': [(False, 'list'), (False, 'form')],
                'domain': [('id', 'in', payment_ids)],
                'name': _('Payments'),
                'target': 'current',
            }

        # Grouped payment (all invoices compatible with journal currency)
        total_amount_pay = 0.0
        total_amount_company = 0.0
        for line in chosen:
            amt_paycur, _res = _clamp_to_residual_paycur(line, line.amount_to_pay or 0.0)
            total_amount_pay += amt_paycur
            total_amount_company += self._pay_to_company(amt_paycur, date)

        if float_compare(total_amount_pay, 0.0, precision_rounding=self._get_payment_currency().rounding) <= 0:
            raise UserError(_("No payments were created. Check the amounts to pay."))

        move_ids = chosen.mapped("move_id").ids
        reg = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=move_ids
        ).create({
            "payment_date": date,
            "journal_id": self.journal_id.id,
            "payment_method_line_id": self.payment_method_line_id.id,
            "currency_id": pay_currency.id,  # display currency
            "amount": total_amount_company,  # amount in company currency (Odoo 19)
            "group_payment": True,
            "communication": self.communication or "",
        })
        payments = reg._create_payments()
        if not payments:
            raise UserError(_("No payments were created. Check the amounts to pay."))

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', 'in', payments.ids)],
            'name': _('Payments'),
            'target': 'current',
        }


class BatchPaymentAllocationWizardLine(models.TransientModel):
    _name = "batch.payment.allocation.wizard.line"
    _description = "Batch Payment Allocation Line"

    wizard_id = fields.Many2one("batch.payment.allocation.wizard", required=True, ondelete="cascade")
    move_id = fields.Many2one("account.move", string="Invoice", required=True, domain="[('state','=','posted')]")
    name = fields.Char(string="Number", readonly=True)
    invoice_date = fields.Date(string="Invoice Date", readonly=True)
    residual_in_payment_currency = fields.Monetary(string="Residual (Payment Currency)", currency_field="currency_id", readonly=True)
    residual_in_company_currency = fields.Monetary(string="Residual (Company Currency)", currency_field="company_currency_id", readonly=True)
    residual_in_invoice_currency = fields.Monetary(string="Residual (Invoice Currency)", currency_field="invoice_currency_id", readonly=True)
    amount_to_pay = fields.Monetary(string="Amount to Pay", currency_field="currency_id")
    currency_id = fields.Many2one(related="wizard_id.payment_currency_id", string="Currency", store=False, readonly=True)
    company_currency_id = fields.Many2one(related="wizard_id.company_id.currency_id", string="Company Currency", store=False, readonly=True)
    invoice_currency_id = fields.Many2one(related="move_id.currency_id", string="Invoice Currency", store=False, readonly=True)

    @api.onchange("move_id")
    def _onchange_move(self):
        for rec in self:
            rec.name = rec.move_id.name or ""
            rec.invoice_date = rec.move_id.invoice_date
            if rec.move_id:
                rec_lines = rec.move_id.line_ids.filtered(lambda l: l.account_id and l.account_id.account_type in ('asset_receivable','liability_payable'))
                residual_company = abs(sum(rec_lines.mapped('amount_residual')))
                residual_invoice = abs(sum(rec_lines.mapped('amount_residual_currency'))) if rec.move_id.currency_id else residual_company
                rec.residual_in_company_currency = residual_company
                rec.residual_in_invoice_currency = residual_invoice
                rec.residual_in_payment_currency = rec.wizard_id._convert_amount(residual_company, rec.wizard_id.payment_date)

    @api.onchange("amount_to_pay")
    def _onchange_amount_to_pay(self):
        for rec in self:
            if rec.amount_to_pay is None:
                continue
            if rec.amount_to_pay < 0:
                rec.amount_to_pay = 0.0

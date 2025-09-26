# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

class BatchPaymentAllocationWizard(models.TransientModel):
    _name = "batch.payment.allocation.wizard"
    _description = "Batch Payment Allocation (One payment -> Many invoices)"

    partner_type = fields.Selection([("customer","Customer"),("supplier","Vendor")], required=True, default="supplier")
    partner_id = fields.Many2one("res.partner", string="Partner", required=True, domain="[('parent_id','=',False)]")
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

    def _convert_amount(self, amount_company_ccy, date):
        self.ensure_one()
        if not amount_company_ccy:
            return 0.0
        if self.rate_source == "custom" and self.custom_rate:
            return amount_company_ccy * self.custom_rate
        return self.env.company.currency_id._convert(amount_company_ccy, self.payment_currency_id, self.company_id,
                                                     date or self.payment_date or fields.Date.context_today(self))

    def _get_payment_currency(self):
        self.ensure_one()
        return self.journal_id.currency_id or self.payment_currency_id or self.company_id.currency_id

    @api.onchange("partner_type", "partner_id", "payment_currency_id", "payment_date", "rate_source", "custom_rate")
    def _onchange_partner(self):
        for w in self:
            w._load_invoices()

    def _load_invoices(self):
        self.ensure_one()
        self.line_ids = [(5, 0, 0)]
        if not (self.partner_type and self.partner_id and self.payment_currency_id):
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
            rec_lines = mv.line_ids.filtered(lambda l: l.account_id and l.account_id.account_type in ('asset_receivable','liability_payable'))
            residual_company = abs(sum(rec_lines.mapped('amount_residual')))
            residual_invoice = abs(sum(rec_lines.mapped('amount_residual_currency'))) if mv.currency_id else residual_company
            if residual_company <= 0 and residual_invoice <= 0:
                continue
            residual_pay_cur = self._convert_amount(residual_company, self.payment_date)
            lines.append((0, 0, {
                'move_id': mv.id,
                'name': mv.name,
                'invoice_date': mv.invoice_date,
                'residual_in_payment_currency': residual_pay_cur,
                'residual_in_company_currency': residual_company,
                'residual_in_invoice_currency': residual_invoice,
                'amount_to_pay': residual_pay_cur,
            }))
        self.line_ids = lines

    @api.depends("line_ids.amount_to_pay")
    def _compute_total_to_pay(self):
        for w in self:
            w.total_to_pay = sum(w.line_ids.mapped("amount_to_pay"))

    def action_remove_selected_lines(self):
        self.ensure_one()
        to_remove = self.line_ids.filtered(lambda l: l.to_delete)
        if to_remove:
            to_remove.unlink()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'batch.payment.allocation.wizard',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new'
        }

    def action_allocate(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("There are no invoice lines to pay."))
        if not self.journal_id:
            raise UserError(_("Please select a Payment Journal."))

        # Default payment method if missing
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

        def _clamp_to_residual_paycur(line, amt_in_wizard_cur):
            residual_company = abs(line.move_id.amount_residual)
            residual_paycur = line.move_id.company_currency_id._convert(residual_company, pay_currency, self.company_id, date)
            amt_paycur = amt_in_wizard_cur
            if self.payment_currency_id != pay_currency:
                amt_paycur = self.payment_currency_id._convert(amt_in_wizard_cur, pay_currency, self.company_id, date)
            if float_compare(amt_paycur, residual_paycur, precision_rounding=pay_currency.rounding) > 0:
                amt_paycur = residual_paycur
            if float_compare(amt_paycur, 0.0, precision_rounding=pay_currency.rounding) < 0:
                amt_paycur = 0.0
            return amt_paycur, residual_paycur

        if self.allocation_mode == "per_invoice":
            payment_ids = []
            for line in chosen:
                amt_wizard_cur = line.amount_to_pay or 0.0
                amt_paycur, _res = _clamp_to_residual_paycur(line, amt_wizard_cur)
                if float_compare(amt_paycur, 0.0, precision_rounding=pay_currency.rounding) <= 0:
                    continue
                reg = self.env["account.payment.register"].with_context(
                    active_model="account.move", active_ids=[line.move_id.id]
                ).create({
                    "payment_date": date,
                    "journal_id": self.journal_id.id,
                    "payment_method_line_id": self.payment_method_line_id.id,
                    "currency_id": pay_currency.id,
                    "amount": amt_paycur,
                    "group_payment": False,
                    "communication": self.communication or "",
                })
                payments = reg._create_payments()
                if not payments:
                    reg.action_create_payments()
                    payments = self.env["account.payment"].search([
                        ("partner_id", "=", self.partner_id.id),
                        ("journal_id", "=", self.journal_id.id),
                        ("date", "=", date),
                        ("amount", "=", amt_paycur),
                    ], order="id desc", limit=1)
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

        # Grouped payment
        total_amount = 0.0
        for line in chosen:
            amt_wizard_cur = line.amount_to_pay or 0.0
            amt_paycur, _res = _clamp_to_residual_paycur(line, amt_wizard_cur)
            total_amount += amt_paycur

        if float_compare(total_amount, 0.0, precision_rounding=pay_currency.rounding) <= 0:
            raise UserError(_("No payments were created. Check the amounts to pay."))

        move_ids = chosen.mapped("move_id").ids
        reg = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=move_ids
        ).create({
            "payment_date": date,
            "journal_id": self.journal_id.id,
            "payment_method_line_id": self.payment_method_line_id.id,
            "currency_id": pay_currency.id,
            "amount": total_amount,
            "group_payment": True,
            "communication": self.communication or "",
        })
        payments = reg._create_payments()
        if not payments:
            reg.action_create_payments()
            payments = self.env["account.payment"].search([
                ("partner_id", "=", self.partner_id.id),
                ("journal_id", "=", self.journal_id.id),
                ("date", "=", date),
                ("amount", "=", total_amount),
            ], order='id desc', limit=1)
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
    amount_to_pay = fields.Monetary(string="Amount to Pay", currency_field="currency_id")
    currency_id = fields.Many2one(related="wizard_id.payment_currency_id", string="Currency", store=False, readonly=True)
    company_currency_id = fields.Many2one(related="wizard_id.company_id.currency_id", string="Company Currency", store=False, readonly=True)
    invoice_currency_id = fields.Many2one(related="move_id.currency_id", string="Invoice Currency", store=False, readonly=True)
    residual_in_company_currency = fields.Monetary(string="Residual (Company Currency)", currency_field="company_currency_id", readonly=True)
    residual_in_invoice_currency = fields.Monetary(string="Residual (Invoice Currency)", currency_field="invoice_currency_id", readonly=True)
    to_delete = fields.Boolean(string="Delete?")

    @api.constrains("amount_to_pay")
    def _check_amount(self):
        for rec in self:
            if rec.amount_to_pay is None:
                continue
            if rec.amount_to_pay < 0:
                raise ValidationError(_("Amount to pay must be >= 0."))

    
    @api.onchange("amount_to_pay")
    def _onchange_amount_to_pay(self):
        for rec in self:
            if rec.amount_to_pay is None:
                continue
            # Compute residual in the payment currency on the fly (don't rely on hidden field)
            move = rec.move_id
            if not move:
                continue
            date = rec.wizard_id.payment_date or fields.Date.context_today(self)
            pay_currency = rec.currency_id or rec.wizard_id.payment_currency_id or rec.wizard_id.company_id.currency_id
            residual_company = abs(move.amount_residual)
            residual_paycur = move.company_currency_id._convert(residual_company, pay_currency, rec.wizard_id.company_id, date)
            # clamp
            if rec.amount_to_pay > residual_paycur:
                rec.amount_to_pay = residual_paycur
            if rec.amount_to_pay < 0:
                rec.amount_to_pay = 0.0


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
                # refresh payment-currency residual via wizard conversion
                rec.residual_in_payment_currency = rec.wizard_id._convert_amount(residual_company, rec.wizard_id.payment_date)

    def action_delete_line(self):
        self.unlink()

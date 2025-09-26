# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero

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
    line_ids = fields.One2many("batch.payment.allocation.wizard.line", "wizard_id", string="Open Items")

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

    def _get_move_line_type(self, aml):
        self.ensure_one()
        move_type = aml.move_id.move_type or 'entry'
        if aml.payment_id:
            return 'payment'
        if move_type in {'out_refund', 'in_refund'}:
            return 'refund'
        if move_type in {'out_invoice', 'in_invoice'}:
            return 'invoice'
        if aml.balance:
            if self.partner_type == 'customer':
                return 'credit' if aml.balance < 0 else 'invoice'
            else:
                return 'credit' if aml.balance > 0 else 'invoice'
        return 'entry'

    def _load_invoices(self):
        self.ensure_one()
        self.line_ids = [(5, 0, 0)]
        if not (self.partner_type and self.partner_id and self.payment_currency_id):
            return
        account_types = ('asset_receivable', 'liability_payable')
        aml_domain = [
            ('partner_id', '=', self.partner_id.id),
            ('company_id', '=', self.company_id.id),
            ('account_id.account_type', 'in', account_types),
            ('move_id.state', '=', 'posted'),
            ('reconciled', '=', False),
        ]
        amls = self.env['account.move.line'].search(aml_domain, order="date asc, move_name asc, id asc")
        lines = []
        for aml in amls:
            residual_company = abs(aml.amount_residual)
            if float_is_zero(residual_company, precision_rounding=self.company_id.currency_id.rounding):
                continue
            residual_invoice = abs(aml.amount_residual_currency) if aml.currency_id else residual_company
            residual_pay_cur = self._convert_amount(residual_company, self.payment_date)
            line_type = self._get_move_line_type(aml)
            is_credit = line_type in {'payment', 'refund', 'credit'}
            lines.append((0, 0, {
                'move_line_id': aml.id,
                'name': aml.move_id.name or aml.name,
                'invoice_date': aml.move_id.invoice_date or aml.date,
                'residual_in_payment_currency': residual_pay_cur,
                'residual_in_company_currency': residual_company,
                'residual_in_invoice_currency': residual_invoice,
                'amount_to_pay': residual_pay_cur,
                'line_type': line_type,
                'is_credit_line': is_credit,
            }))
        self.line_ids = lines

    @api.depends("line_ids.amount_to_pay")
    def _compute_total_to_pay(self):
        for w in self:
            debit_total = sum(w.line_ids.filtered(lambda l: not l.is_credit_line).mapped("amount_to_pay"))
            credit_total = sum(w.line_ids.filtered(lambda l: l.is_credit_line).mapped("amount_to_pay"))
            w.total_to_pay = debit_total - credit_total

    def _apply_existing_entries(self, invoice_lines, credit_lines):
        self.ensure_one()
        if not invoice_lines:
            raise UserError(_("Please select at least one document to apply the available credits."))

        company_currency = self.company_id.currency_id
        company_rounding = company_currency.rounding
        pay_currency = self.payment_currency_id

        all_lines = invoice_lines | credit_lines
        debit_entries = []
        credit_entries = []
        prepared = []

        for line in all_lines:
            aml = line.move_line_id
            if not aml:
                continue
            residual_company = abs(aml.amount_residual)
            if float_is_zero(residual_company, precision_rounding=company_rounding):
                continue
            residual_paycur = self._convert_amount(residual_company, self.payment_date)
            if float_is_zero(residual_paycur, precision_rounding=pay_currency.rounding):
                continue
            amt_to_use = line.amount_to_pay or 0.0
            if float_compare(amt_to_use, 0.0, precision_rounding=pay_currency.rounding) <= 0:
                continue
            ratio = min(1.0, amt_to_use / residual_paycur) if residual_paycur else 0.0
            if float_is_zero(ratio, precision_digits=6):
                continue
            company_total = residual_company * ratio
            currency_total = aml.amount_residual_currency * ratio if aml.currency_id else 0.0
            data = {
                'wizard_line': line,
                'aml': aml,
                'company_total': company_total,
                'company_remaining': company_total,
                'currency_total': currency_total,
                'currency_remaining': currency_total,
                'currency_ratio': (currency_total / company_total) if company_total else 0.0,
                'wizard_total': amt_to_use,
                'wizard_remaining': amt_to_use,
                'wizard_ratio': (amt_to_use / company_total) if company_total else 0.0,
                'applied_company': 0.0,
            }
            prepared.append(data)
            if aml.balance > 0:
                debit_entries.append(data)
            else:
                credit_entries.append(data)

        if not debit_entries or not credit_entries:
            return {line.id: line.amount_to_pay for line in invoice_lines}

        partial_vals = []

        while debit_entries and credit_entries:
            debit = debit_entries[0]
            credit = credit_entries[0]

            take = min(debit['company_remaining'], credit['company_remaining'])
            take = company_currency.round(take)
            if float_is_zero(take, precision_rounding=company_rounding):
                break

            debit_currency_take = 0.0
            credit_currency_take = 0.0

            if debit['aml'].currency_id:
                debit_currency_take = debit['currency_ratio'] * take
            if credit['aml'].currency_id:
                credit_currency_take = credit['currency_ratio'] * take

            debit['company_remaining'] -= take
            credit['company_remaining'] -= take
            debit['applied_company'] += take
            credit['applied_company'] += take

            if debit['wizard_ratio']:
                debit_wizard_take = debit['wizard_ratio'] * take
            else:
                debit_wizard_take = 0.0
            if credit['wizard_ratio']:
                credit_wizard_take = credit['wizard_ratio'] * take
            else:
                credit_wizard_take = 0.0

            debit['wizard_remaining'] -= debit_wizard_take
            credit['wizard_remaining'] -= credit_wizard_take

            if debit['aml'].currency_id:
                debit['currency_remaining'] -= debit['aml'].currency_id.round(debit_currency_take)
            if credit['aml'].currency_id:
                credit['currency_remaining'] -= credit['aml'].currency_id.round(credit_currency_take)

            vals = {
                'debit_move_id': debit['aml'].id,
                'credit_move_id': credit['aml'].id,
                'amount': take,
                'company_id': self.company_id.id,
                'company_currency_id': company_currency.id,
            }

            debit_currency_amount = debit['aml'].currency_id.round(debit_currency_take) if debit['aml'].currency_id else 0.0
            credit_currency_amount = credit['aml'].currency_id.round(credit_currency_take) if credit['aml'].currency_id else 0.0

            currency_id = False
            if debit['aml'].currency_id and credit['aml'].currency_id and debit['aml'].currency_id == credit['aml'].currency_id:
                currency_id = debit['aml'].currency_id.id
            elif debit['aml'].currency_id and not credit['aml'].currency_id:
                currency_id = debit['aml'].currency_id.id
                credit_currency_amount = 0.0
            elif credit['aml'].currency_id and not debit['aml'].currency_id:
                currency_id = credit['aml'].currency_id.id
                debit_currency_amount = 0.0

            if currency_id:
                vals.update({
                    'currency_id': currency_id,
                    'debit_amount_currency': debit_currency_amount,
                    'credit_amount_currency': credit_currency_amount,
                })

            partial_vals.append(vals)

            if float_is_zero(debit['company_remaining'], precision_rounding=company_rounding):
                debit_entries.pop(0)
            if float_is_zero(credit['company_remaining'], precision_rounding=company_rounding):
                credit_entries.pop(0)

        if not partial_vals:
            return {line.id: line.amount_to_pay for line in invoice_lines}

        self.env['account.partial.reconcile'].create(partial_vals)

        remaining_map = {}
        for line in invoice_lines:
            data = next((d for d in prepared if d['wizard_line'] == line), None)
            if data:
                remaining = max(data['wizard_remaining'], 0.0)
            else:
                remaining = line.amount_to_pay
            remaining_map[line.id] = remaining

        return remaining_map

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

        invoice_lines = chosen.filtered(lambda l: not l.is_credit_line)
        credit_lines = chosen - invoice_lines

        if credit_lines:
            remaining_map = self._apply_existing_entries(invoice_lines, credit_lines)
            invoice_lines = invoice_lines.filtered(lambda l: float_compare(
                remaining_map.get(l.id, 0.0), 0.0,
                precision_rounding=self.payment_currency_id.rounding
            ) > 0)
            for line in invoice_lines:
                new_amount = remaining_map.get(line.id, 0.0)
                line.amount_to_pay = new_amount
                residual_company = abs(line.move_line_id.amount_residual)
                residual_invoice = abs(line.move_line_id.amount_residual_currency) if line.move_line_id.currency_id else residual_company
                line.residual_in_company_currency = residual_company
                line.residual_in_invoice_currency = residual_invoice
                line.residual_in_payment_currency = self._convert_amount(residual_company, self.payment_date)
            chosen = invoice_lines
            if not chosen:
                return {'type': 'ir.actions.act_window_close'}

        def _clamp_to_residual_paycur(line, amt_in_wizard_cur):
            residual_company = abs(line.move_line_id.amount_residual)
            residual_paycur = line.move_line_id.company_currency_id._convert(
                residual_company, pay_currency, self.company_id, date
            )
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
    move_line_id = fields.Many2one(
        "account.move.line",
        string="Open Item",
        required=True,
        domain="[('move_id.state','=','posted'),('reconciled','=',False),('partner_id','=',wizard_id.partner_id),('company_id','=',wizard_id.company_id),\n                 ('account_id.account_type','in',('asset_receivable','liability_payable'))]",
    )
    move_id = fields.Many2one(related="move_line_id.move_id", string="Journal Entry", store=False, readonly=True)
    name = fields.Char(string="Number", readonly=True)
    line_type = fields.Selection([
        ('invoice', 'Invoice / Bill'),
        ('refund', 'Credit Note'),
        ('payment', 'Payment'),
        ('credit', 'Credit Entry'),
        ('entry', 'Journal Entry'),
    ], string="Type", readonly=True)
    is_credit_line = fields.Boolean(string="Is Credit", readonly=True)
    invoice_date = fields.Date(string="Document Date", readonly=True)
    residual_in_payment_currency = fields.Monetary(string="Residual (Payment Currency)", currency_field="currency_id", readonly=True)
    amount_to_pay = fields.Monetary(string="Amount to Pay", currency_field="currency_id")
    currency_id = fields.Many2one(related="wizard_id.payment_currency_id", string="Currency", store=False, readonly=True)
    company_currency_id = fields.Many2one(related="wizard_id.company_id.currency_id", string="Company Currency", store=False, readonly=True)
    invoice_currency_id = fields.Many2one(related="move_line_id.currency_id", string="Item Currency", store=False, readonly=True)
    residual_in_company_currency = fields.Monetary(string="Residual (Company Currency)", currency_field="company_currency_id", readonly=True)
    residual_in_invoice_currency = fields.Monetary(string="Residual (Original Currency)", currency_field="invoice_currency_id", readonly=True)
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
            move_line = rec.move_line_id
            if not move_line:
                continue
            date = rec.wizard_id.payment_date or fields.Date.context_today(self)
            pay_currency = rec.currency_id or rec.wizard_id.payment_currency_id or rec.wizard_id.company_id.currency_id
            residual_company = abs(move_line.amount_residual)
            residual_paycur = move_line.company_currency_id._convert(residual_company, pay_currency, rec.wizard_id.company_id, date)
            # clamp
            if rec.amount_to_pay > residual_paycur:
                rec.amount_to_pay = residual_paycur
            if rec.amount_to_pay < 0:
                rec.amount_to_pay = 0.0


    @api.onchange("move_line_id")
    def _onchange_move(self):
        for rec in self:
            aml = rec.move_line_id
            if not aml:
                continue
            rec.name = aml.move_id.name or aml.name or ""
            rec.invoice_date = aml.move_id.invoice_date or aml.date
            residual_company = abs(aml.amount_residual)
            residual_invoice = abs(aml.amount_residual_currency) if aml.currency_id else residual_company
            rec.residual_in_company_currency = residual_company
            rec.residual_in_invoice_currency = residual_invoice
            rec.residual_in_payment_currency = rec.wizard_id._convert_amount(residual_company, rec.wizard_id.payment_date)
            rec.amount_to_pay = rec.residual_in_payment_currency
            rec.line_type = rec.wizard_id._get_move_line_type(aml)
            rec.is_credit_line = rec.line_type in {'payment', 'refund', 'credit'}

    def action_delete_line(self):
        self.unlink()

# Part of Odoo. See LICENSE file for full copyright and licensing details.

import hashlib
import hmac
import json
import time

from contextlib import contextmanager
from unittest.mock import patch

from odoo.addons.mail.tests.common import MailCommon, mail_new_test_user
from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
from odoo.addons.whatsapp.models.whatsapp_message import WhatsAppMessage
from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError
from odoo.tests import common


class MockOutgoingWhatsApp(common.BaseCase):
    """ Mock calls to WhatsApp API, provide tools and patch to know what happens
    when contacting it. """

    @contextmanager
    def mockWhatsappGateway(self):
        self._init_wa_mock()
        wa_msg_origin = WhatsAppMessage.create

        def _upload_whatsapp_document(attachment):
            if attachment:
                return {
                    "messaging_product": "whatsapp",
                    "contacts": [{
                            "input": self.whatsapp_account,
                            "wa_id": "1234567890",
                        }],
                    "messages": [{
                        "id": "qwertyuiop0987654321",
                    }]
                }
            raise WhatsAppError("Please ensure you are using the correct file type and try again.")

        def _send_whatsapp(number, *, send_vals, **kwargs):
            if send_vals:
                msg_uid = f'test_wa_{time.time():.9f}'
                self._wa_msg_sent.append(msg_uid)
                return msg_uid
            raise WhatsAppError("Please make sure to define a template before proceeding.")

        def _wa_message_create(model, *args, **kwargs):
            res = wa_msg_origin(model, *args, **kwargs)
            self._new_wa_msg += res.sudo()
            return res

        try:
            with patch.object(WhatsAppApi, '_upload_whatsapp_document', side_effect=_upload_whatsapp_document), \
                 patch.object(WhatsAppApi, '_send_whatsapp', side_effect=_send_whatsapp), \
                 patch.object(WhatsAppMessage, 'create', autospec=True, wraps=WhatsAppMessage, side_effect=_wa_message_create):
                yield
        finally:
            pass

    def _init_wa_mock(self):
        self._new_wa_msg = self.env['whatsapp.message'].sudo()
        self._wa_msg_sent = []


class MockIncomingWhatsApp(common.HttpCase):
    """ Mock and provide tools on incoming WhatsApp calls. """

    # ------------------------------------------------------------
    # TOOLS FOR SIMULATING RECEPTION
    # ------------------------------------------------------------

    def _receive_whatsapp_message(self, account, body, sender_phone_number, additional_message_values=None):
        message_data = json.dumps({
            "entry": [{
                "id": account.account_uid,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "metadata": {"phone_number_id": account.phone_uid},
                        "messages": [
                            dict({
                                "id": f"test_wa_{time.time():.9f}",
                                "from": sender_phone_number,
                                "type": "text",
                                "text": {"body": body}
                            }, **(additional_message_values or {}))
                        ],
                    }
                }]
            }]
        })

        message_signature = hmac.new(
            self.whatsapp_account.app_secret.encode(),
            msg=message_data.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        return self._make_webhook_request(
            account,
            message_data=message_data,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=%s" % message_signature,
            }
        )

    def _make_webhook_request(self, account, message_data=None, headers=None):
        if not message_data:
            message_data = json.dumps({'entry': [{'id': account.account_uid}]}).encode()
        return self.url_open(
            '/whatsapp/webhook/', data=message_data, headers={
                "Content-Type": "application/json",
                **(headers or {})
            }
        ).json()

    # ------------------------------------------------------------
    # TEST TOOLS AND ASSERTS
    # ------------------------------------------------------------

    def _find_discuss_channel(self, whatsapp_number):
        return self.env["discuss.channel"].search([("whatsapp_number", "=", whatsapp_number)])

    def assertWhatsAppChannel(self, sender_phone_number):
        discuss_channel = self._find_discuss_channel(sender_phone_number)
        self.assertEqual(len(discuss_channel), 1, f'Should find exactly one channel for number {sender_phone_number}')
        self.assertEqual(len(discuss_channel.message_ids), 1)
        return discuss_channel


class WhatsAppCase(MockOutgoingWhatsApp):
    """ Common class with tools and asserts """

    # ------------------------------------------------------------
    # TOOLS
    # ------------------------------------------------------------

    def _add_button_to_template(self, template, name, sequence=1, call_number=False, url_type=False, button_type=False, website_url=False):
        template.write({
            'button_ids': [(0, 0, {
                'button_type': button_type if button_type else 'quick_reply',
                'call_number': call_number if call_number else '',
                'name': name,
                'sequence': sequence,
                'url_type': url_type if url_type else 'static',
                'wa_template_id': template.id,
                'website_url': website_url if website_url else '',
            })],
        })

    def _instanciate_wa_composer_from_records(self, template, from_records, with_user=False):
        return self.env['whatsapp.composer'].with_context({
            'active_model': from_records._name,
            'active_ids': from_records.ids,
        }).with_user(with_user or self.env.user).create({
            'wa_template_id': template.id,
        })

    # ------------------------------------------------------------
    # MESSAGE FIND AND ASSERTS
    # ------------------------------------------------------------

    def _find_wa_msg_wrecord(self, record):
        for wa_msg in self._new_wa_msg:
            if wa_msg.mail_message_id.model == record._name and wa_msg.mail_message_id.res_id == record.id:
                break
        else:
            debug_info = '\n'.join(
                f'From: {wa_msg.id}'
                for wa_msg in self._new_wa_msg
            )
            raise AssertionError(
                f'whatsapp.message not found for record {record.display_name} ({record._name}/{record.id}\n{debug_info}'
            )
        return wa_msg

    def _assertWAMessage(self, wa_message, status='sent', fields_values=None, attachment_values=None):
        """ Assert content of WhatsApp message.

        :param <whatsapp.message> wa_message: whatsapp message whose content
          is going to be checked;
        :param str status: one of whatsapp.message.state field value;
        :param dict fields_values: if given, should be a dictionary of field
          names / values allowing to check message content (e.g. body);
        :param dict attachment_values: if given, should be a dictionary of field
          names / values allowing to check attachment values (e.g. mimetype);
        """
        if len(wa_message) != 1:
            debug_info = '\n'.join(
                f'Msg: {wa_msg.id}, {wa_msg.body}'
                for wa_msg in wa_message
            )
            raise AssertionError(
                f'whatsapp.message: should have 1 message, received {len(wa_message)}\n{debug_info}'
            )

        # check base message data
        self.assertEqual(wa_message.state, status,
                         f'whatsapp.message invalid status: found {wa_message.state}, expected {status}')

        # check message content
        for fname, fvalue in (fields_values or {}).items():
            with self.subTest(fname=fname, fvalue=fvalue):
                self.assertEqual(
                    wa_message[fname], fvalue,
                    f'whatsapp.message: expected {fvalue} for {fname}, got {wa_message[fname]}'
                )

        if attachment_values:
            # check attachment values
            attachment = wa_message.mail_message_id.attachment_ids
            # only support one attachment for whatsapp messages
            self.assertEqual(len(attachment), 1)

            for fname, fvalue in (attachment_values).items():
                with self.subTest(fname=fname, fvalue=fvalue):
                    attachment_value = attachment[fname]
                    self.assertEqual(
                        attachment_value, fvalue,
                        f'whatsapp.message invalid attachment: expected {fvalue} for {fname}, got {attachment_value}'
                    )

    def assertWAMessage(self, status='sent', fields_values=None, attachment_values=None):
        self._assertWAMessage(self._new_wa_msg, status=status, fields_values=fields_values, attachment_values=attachment_values)

    def assertWAMessageFromRecord(self, record, status='sent', fields_values=None, attachment_values=None):
        whatsapp_message = self._find_wa_msg_wrecord(record)
        self._assertWAMessage(whatsapp_message, status=status, fields_values=fields_values, attachment_values=attachment_values)

    # ------------------------------------------------------------
    # OTHER MODELS ASSERTS
    # ------------------------------------------------------------

    def assertTemplateVariables(self, template, expected_variables):
        for expected in expected_variables:
            exp_name, exp_line_type, exp_field_type, exp_vals = expected
            with self.subTest(exp_name=exp_name):
                tpl_variable = template.variable_ids.filtered(
                    lambda v: v.name == exp_name
                )
                self.assertTrue(tpl_variable)
                self.assertEqual(tpl_variable.line_type, exp_line_type)
                self.assertEqual(tpl_variable.field_type, exp_field_type)
                for fname, fvalue in (exp_vals or {}).items():
                    self.assertEqual(tpl_variable[fname], fvalue)


class WhatsAppCommon(MailCommon, WhatsAppCase):
    """ Bootstrap data for tests """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # phone-specific test data
        cls.user_employee_mobile = '+91(132)-553-7272'
        cls.user_employee.mobile = cls.user_employee_mobile

        # Notified user for WhatsApp Business Account
        cls.user_wa_admin = mail_new_test_user(
            cls.env,
            company_id=cls.company_admin.id,
            country_id=cls.env.ref('base.in').id,
            email='wa_admin@test.example.com',
            groups='base.group_user,base.group_partner_manager,whatsapp.group_whatsapp_admin',
            login='user_wa_admin',
            mobile='+91(132)-553-7242',
            name='WhatsApp Wasin',
            notification_type='email',
            phone='+1 650-555-0111',
            signature='--\nWasin'
        )
        # WhatsApp Business Account
        cls.whatsapp_account = cls.env['whatsapp.account'].with_user(cls.user_admin).create({
            'account_uid': 'abcdef123456',
            'app_secret': '1234567890abcdef',
            'app_uid': 'contact',
            'name': 'odoo account',
            'notify_user_ids': cls.user_wa_admin.ids,
            'phone_uid': '1234567890',
            'token': 'team leader',
        })
        # Test customer (In)
        cls.whatsapp_customer = cls.env['res.partner'].create({
            'country_id': cls.env.ref('base.in').id,
            'email': 'wa.customer.in@test.example.com',
            'name': 'Wa Customer In',
            'phone': "+91 12345 67891"
        })

        # https://github.com/mathiasbynens/small
        image_data = ("/9j/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8Q"
                      "EBEQCgwSExIQEw8QEBD/yQALCAABAAEBAREA/8wABgAQEAX/2gAIAQEAAD8A0s8g/9k=")
        pdf_data = ("JVBERi0xLgoxIDAgb2JqPDwvUGFnZXMgMiAwIFI+PmVuZG9iagoyIDAgb2JqPDwvS2lkc1szIDAg"
                    "Ul0vQ291bnQgMT4+ZW5kb2JqCjMgMCBvYmo8PC9QYXJlbnQgMiAwIFI+PmVuZG9iagp0cmFpbGVy"
                    "IDw8L1Jvb3QgMSAwIFI+Pg==")
        video_data = ("AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAAIZnJlZQAAAAhtZGF0AAAA1m1vb3YA"
                      "AABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAAAAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAA"
                      "AAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAABidWR0"
                      "YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAA"
                      "Jal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjU3LjQxLjEwMA==")
        documents = cls.env['ir.attachment'].with_user(cls.user_employee).create([
            {'name': 'Document.pdf', 'datas': pdf_data, 'mimetype': 'application/pdf'},
            {'name': 'Image.jpg', 'datas': image_data, 'mimetype': 'image/jpeg'},
            {'name': 'Video.mpg', 'datas': video_data, 'mimetype': 'video/mp4'},
            {'name': 'Payload.wasm', 'datas': "AGFzbQEAAAA=", 'mimetype': 'application/octet-stream'},
        ])
        cls.document_attachment, cls.image_attachment, cls.video_attachment, cls.invalid_attachment = documents

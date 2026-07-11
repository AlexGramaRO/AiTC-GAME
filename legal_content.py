"""
Legal documents shown during sign-up and on standalone policy pages.

Update CURRENT_*_VERSION when content changes so new sign-ups must re-accept.
"""

CURRENT_TERMS_VERSION = '2026-07-11'
CURRENT_REFUND_POLICY_VERSION = '2026-07-11'


def get_terms_sections():
    return [
        {
            'title': '1. Agreement and beta status',
            'paragraphs': [
                'These Terms and Conditions ("Terms") govern access to and use of the webATC® platform, website, and related services (collectively, the "Service") operated by the provider of webATC ("we", "us", "our"). By creating an account, you agree to be bound by these Terms.',
                'The Service is currently offered as a beta test. Features, availability, performance, and functionality may change, be limited, or be withdrawn at any time without prior notice. You acknowledge that the Service may contain errors, interruptions, or incomplete functionality.',
            ],
        },
        {
            'title': '2. Eligibility and account registration',
            'paragraphs': [
                'You must provide accurate, current, and complete registration information. You are responsible for maintaining the confidentiality of your credentials and for all activity under your account.',
                'Account approval, activation, and continued access may require administrator review. We may approve, reject, suspend, or terminate any account at our sole discretion.',
            ],
        },
        {
            'title': '3. Acceptable use',
            'paragraphs': [
                'You may use the Service only for lawful purposes and in accordance with these Terms. Without limitation, you must not:',
                '• misuse, disrupt, overload, reverse engineer, scrape, probe, or attempt to gain unauthorized access to the Service, its systems, or other users\' accounts;',
                '• use automated tools, bots, scripts, or similar means to abuse simulator capacity, billing, promotion codes, or authentication mechanisms;',
                '• share, resell, sublicense, or transfer access credentials or paid access rights except as expressly permitted by us;',
                '• upload, transmit, or use content that is unlawful, harmful, fraudulent, infringing, harassing, or otherwise objectionable;',
                '• interfere with the operation of the Service or with other users\' use of the Service;',
                '• misrepresent your identity, affiliation, or authority.',
                'Any deviation from acceptable use, abuse, attempted abuse, or conduct we reasonably consider harmful to the Service, other users, or our business interests constitutes a material breach of these Terms.',
            ],
        },
        {
            'title': '4. Suspension and termination',
            'paragraphs': [
                'We may suspend, restrict, or terminate your account immediately, with or without notice, if we believe you have violated these Terms, engaged in misuse or abuse, created risk or legal exposure for us, or for any other reason at our sole discretion.',
                'Upon suspension or termination for cause, including misuse or abuse, your account may be immediately deleted or permanently disabled. You may lose access to the Service and any associated data without compensation.',
                'Where your account is terminated for cause, any active subscription, pass, or promotional access may be cancelled immediately. Amounts already paid for the Service may be forfeited to the fullest extent permitted by applicable law, without refund, credit, or compensation of any kind.',
            ],
        },
        {
            'title': '5. Paid access',
            'paragraphs': [
                'Certain features require paid access, including monthly subscriptions, One Day Passes, or access activated through promotion codes. Pricing, billing intervals, and access duration are displayed at the point of purchase or redemption.',
                'You authorize us and our payment processors to charge applicable fees using the payment method you provide. You are responsible for all applicable taxes, charges, and payment-related fees unless stated otherwise.',
                'Failure to comply with these Terms may result in immediate cancellation of paid access without refund.',
            ],
        },
        {
            'title': '6. Intellectual property',
            'paragraphs': [
                'The Service, including software, content, branding, designs, and documentation, is owned by us or our licensors and is protected by intellectual property laws. Except for the limited right to use the Service in accordance with these Terms, no rights are granted to you.',
                'You must not copy, modify, distribute, sell, lease, or create derivative works from any part of the Service except as expressly authorized in writing by us.',
            ],
        },
        {
            'title': '7. Disclaimer of warranties',
            'paragraphs': [
                'THE SERVICE IS PROVIDED ON AN "AS IS" AND "AS AVAILABLE" BASIS. TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, WE DISCLAIM ALL WARRANTIES, WHETHER EXPRESS, IMPLIED, STATUTORY, OR OTHERWISE, INCLUDING ANY IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT.',
                'We do not warrant that the Service will be uninterrupted, secure, error-free, accurate, or suitable for training, operational, regulatory, or professional certification purposes.',
            ],
        },
        {
            'title': '8. Limitation of liability',
            'paragraphs': [
                'TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, WE AND OUR AFFILIATES, OFFICERS, DIRECTORS, EMPLOYEES, AGENTS, AND SUPPLIERS SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, OR ANY LOSS OF PROFITS, REVENUE, DATA, GOODWILL, OR BUSINESS OPPORTUNITY, ARISING OUT OF OR RELATED TO YOUR USE OF OR INABILITY TO USE THE SERVICE.',
                'TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, OUR TOTAL AGGREGATE LIABILITY FOR ANY CLAIM ARISING OUT OF OR RELATING TO THE SERVICE OR THESE TERMS SHALL NOT EXCEED THE GREATER OF (A) THE AMOUNT YOU PAID US FOR THE SERVICE IN THE TWELVE (12) MONTHS BEFORE THE EVENT GIVING RISE TO THE CLAIM, OR (B) ONE HUNDRED EUROS (EUR 100).',
            ],
        },
        {
            'title': '9. Changes',
            'paragraphs': [
                'We may modify these Terms at any time. Updated Terms will be posted on the Service with a revised version date. Continued use of the Service after changes become effective constitutes acceptance of the revised Terms, except where applicable law requires explicit re-acceptance.',
            ],
        },
        {
            'title': '10. Governing law',
            'paragraphs': [
                'These Terms are governed by the laws applicable in the jurisdiction where the operator of webATC is established, without regard to conflict-of-law principles. Mandatory consumer protection laws in your country of residence remain unaffected where they apply and cannot be waived by these Terms.',
                'If any provision of these Terms is held invalid or unenforceable, the remaining provisions will remain in full force and effect.',
            ],
        },
    ]


def get_refund_policy_sections():
    return [
        {
            'title': '1. General policy',
            'paragraphs': [
                'This Refund Policy applies to all purchases of paid access to the webATC® platform, including monthly subscriptions, One Day Passes, and any other paid access products we offer.',
                'Except where mandatory applicable law requires otherwise, all payments are final and non-refundable.',
            ],
        },
        {
            'title': '2. No refunds once access is granted',
            'paragraphs': [
                'When you purchase paid access, we immediately make platform access available to your account for the applicable period, whether or not you actually use the Service during that period.',
                'Because access is provisioned and made available upon purchase or activation, you acknowledge and agree that no refund, partial refund, credit, chargeback remedy, or compensation will be provided for:',
                '• unused access time;',
                '• partial use of the Service;',
                '• dissatisfaction with beta functionality or feature availability;',
                '• account suspension or termination resulting from your breach of the Terms and Conditions;',
                '• promotion codes, discounted access, or administrator-granted access once activated.',
            ],
        },
        {
            'title': '3. Subscriptions and passes',
            'paragraphs': [
                'Monthly subscriptions may be cancelled to stop future renewal, but cancellation does not entitle you to a refund for the current billing period or any prior period.',
                'One Day Passes and time-limited promotional access begin when activated and expire automatically. No refund is available after activation, regardless of actual usage.',
            ],
        },
        {
            'title': '4. Abuse, misuse, and termination',
            'paragraphs': [
                'If your account is suspended, restricted, or deleted due to misuse, abuse, or breach of the Terms and Conditions, all paid access may be cancelled immediately and all amounts paid are forfeited without refund to the fullest extent permitted by applicable law.',
            ],
        },
        {
            'title': '5. Payment processor disputes',
            'paragraphs': [
                'You agree to contact us before initiating a payment dispute or chargeback where permitted. Improper chargebacks or payment disputes submitted in breach of this policy may result in immediate account termination and permanent loss of access.',
            ],
        },
        {
            'title': '6. Mandatory legal rights',
            'paragraphs': [
                'Nothing in this Refund Policy limits any non-waivable statutory rights you may have under mandatory consumer protection law in your country of residence. Where such law grants you a right of withdrawal or refund that cannot be excluded by contract, that mandatory right prevails over this policy to the minimum extent required.',
            ],
        },
        {
            'title': '7. Changes',
            'paragraphs': [
                'We may update this Refund Policy at any time by posting a revised version on the Service. The version in effect at the time of your purchase or account registration applies unless a newer version is expressly accepted by you.',
            ],
        },
    ]


def get_legal_payload():
    return {
        'termsVersion': CURRENT_TERMS_VERSION,
        'refundPolicyVersion': CURRENT_REFUND_POLICY_VERSION,
        'termsTitle': 'Terms and Conditions',
        'refundPolicyTitle': 'Refund Policy',
        'termsSections': get_terms_sections(),
        'refundPolicySections': get_refund_policy_sections(),
    }

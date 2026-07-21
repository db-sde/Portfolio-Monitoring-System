"""
Static, fully-fictitious demo data so a visitor can see what casparser-web
does before trusting it with a real statement. Names/PAN/emails here are
placeholder examples (the same generic "ABCDE1234F"-style dummies used in
official Indian tax documents), never sourced from a real upload.
"""

SAMPLE_MF = {
    "statement_period": {"from": "2024-04-01", "to": "2025-03-31"},
    "file_type": "CAMS",
    "cas_type": "DETAILED",
    "investor_info": {
        "name": "Rahul Sharma",
        "email": "rahul.sharma@example.com",
        "address": "12, MG Road, Bengaluru 560001",
        "mobile": "+9199XXXXXX21",
    },
    "parse_warnings": [],
    "folios": [
        {
            "amc": "HDFC Mutual Fund", "folio": "12345678 / 90", "PAN": "ABCDE1234F", "KYC": "OK", "PANKYC": "OK",
            "schemes": [{
                "scheme": "HDFC Flexi Cap Fund - Direct Plan - Growth", "type": "EQUITY",
                "rta": "CAMS", "rta_code": "118834", "isin": "INF179K01VY8", "amfi": "118834",
                "open": 0, "close": 126.55, "close_calculated": 126.55, "nominees": [],
                "valuation": {"date": "2025-03-31", "nav": 165.41, "cost": 30000, "value": 34803},
                "transactions": [
                    {"date": "2024-04-02", "description": "SIP Purchase - Instalment 1/12", "amount": 5000, "units": 33.412, "nav": 149.64, "balance": 33.412, "type": "PURCHASE_SIP"},
                    {"date": "2024-04-02", "description": "*** Stamp Duty ***", "amount": 0.25, "units": None, "nav": None, "balance": None, "type": "STAMP_DUTY_TAX"},
                    {"date": "2024-07-02", "description": "SIP Purchase - Instalment 4/12", "amount": 5000, "units": 32.100, "nav": 155.76, "balance": 65.512, "type": "PURCHASE_SIP"},
                    {"date": "2024-10-01", "description": "SIP Purchase - Instalment 7/12", "amount": 5000, "units": 31.000, "nav": 161.29, "balance": 96.512, "type": "PURCHASE_SIP"},
                    {"date": "2025-01-02", "description": "SIP Purchase - Instalment 10/12", "amount": 5000, "units": 30.400, "nav": 164.47, "balance": 126.912, "type": "PURCHASE_SIP"},
                    {"date": "2025-02-15", "description": "Redemption", "amount": -5620, "units": -30.150, "nav": 186.40, "balance": 96.762, "type": "REDEMPTION"},
                    {"date": "2025-03-03", "description": "SIP Purchase - Instalment 12/12", "amount": 5000, "units": 29.788, "nav": 167.85, "balance": 126.550, "type": "PURCHASE_SIP"},
                ],
            }],
        },
        {
            "amc": "Axis Mutual Fund", "folio": "9988776655", "PAN": "ABCDE1234F", "KYC": "OK", "PANKYC": "OK",
            "schemes": [{
                "scheme": "Axis Bluechip Fund - Direct Plan - Growth", "type": "EQUITY",
                "rta": "CAMS", "rta_code": "120465", "isin": "INF846K01EW2", "amfi": "120465",
                "open": 0, "close": 512.33, "close_calculated": 512.33, "nominees": [],
                "valuation": {"date": "2025-03-31", "nav": 62.18, "cost": 28000, "value": 31856},
                "transactions": [
                    {"date": "2024-05-10", "description": "Purchase", "amount": 15000, "units": 255.102, "nav": 58.80, "balance": 255.102, "type": "PURCHASE"},
                    {"date": "2024-05-10", "description": "*** Stamp Duty ***", "amount": 0.75, "units": None, "nav": None, "balance": None, "type": "STAMP_DUTY_TAX"},
                    {"date": "2024-11-12", "description": "Purchase", "amount": 13000, "units": 257.228, "nav": 50.53, "balance": 512.330, "type": "PURCHASE"},
                ],
            }],
        },
        {
            "amc": "ICICI Prudential Mutual Fund", "folio": "556677 / 88", "PAN": "ABCDE1234F", "KYC": "OK", "PANKYC": "OK",
            "schemes": [
                {
                    "scheme": "ICICI Prudential Bluechip Fund - Direct Plan - Growth", "type": "EQUITY",
                    "rta": "CAMS", "rta_code": "120586", "isin": "INF109K01BL4", "amfi": "120586",
                    "open": 0, "close": 498.70, "close_calculated": 498.70, "nominees": [],
                    "valuation": {"date": "2025-03-31", "nav": 104.90, "cost": 45000, "value": 52310},
                    "transactions": [
                        {"date": "2024-04-15", "description": "Purchase", "amount": 45000, "units": 498.700, "nav": 90.23, "balance": 498.700, "type": "PURCHASE"},
                        {"date": "2024-04-15", "description": "*** Stamp Duty ***", "amount": 2.25, "units": None, "nav": None, "balance": None, "type": "STAMP_DUTY_TAX"},
                    ],
                },
                {
                    "scheme": "ICICI Prudential Liquid Fund - Direct Plan - Growth", "type": "DEBT",
                    "rta": "CAMS", "rta_code": "120200", "isin": "INF109K01VQ1", "amfi": "120200",
                    "open": 0, "close": 57.85, "close_calculated": 57.85, "nominees": [],
                    "valuation": {"date": "2025-03-31", "nav": 361.02, "cost": 20000, "value": 20890},
                    "transactions": [
                        {"date": "2023-05-10", "description": "Purchase", "amount": 20000, "units": 60.500, "nav": 330.50, "balance": 60.500, "type": "PURCHASE"},
                        {"date": "2024-11-20", "description": "Redemption", "amount": -15980, "units": -45.300, "nav": 352.70, "balance": 15.200, "type": "REDEMPTION"},
                        {"date": "2024-11-20", "description": "STT Paid", "amount": 0.02, "units": None, "nav": None, "balance": None, "type": "STT_TAX"},
                    ],
                },
            ],
        },
        {
            "amc": "SBI Mutual Fund", "folio": "445566778", "PAN": "ABCDE1234F", "KYC": "OK", "PANKYC": "OK",
            "schemes": [{
                "scheme": "SBI Small Cap Fund - Direct Plan - Growth", "type": "EQUITY",
                "rta": "CAMS", "rta_code": "125497", "isin": "INF200K01T51", "amfi": "125497",
                "open": 0, "close": 142.60, "close_calculated": 142.60, "nominees": [],
                "valuation": {"date": "2025-03-31", "nav": 152.52, "cost": 25000, "value": 21750},
                "transactions": [
                    {"date": "2023-07-01", "description": "Purchase", "amount": 25000, "units": 190.400, "nav": 131.30, "balance": 190.400, "type": "PURCHASE"},
                    {"date": "2025-01-10", "description": "Redemption", "amount": -7100, "units": -47.800, "nav": 148.50, "balance": 142.600, "type": "REDEMPTION"},
                ],
            }],
        },
    ],
}

SAMPLE_MF_GAINS = [
    {
        "fy": "2024-25", "scheme": "HDFC Flexi Cap Fund - Direct Plan - Growth", "folio": "12345678 / 90",
        "isin": "INF179K01VY8", "fund_type": "EQUITY",
        "purchase_date": "2024-04-02", "purchase_nav": 149.64, "purchase_value": 4512.0, "stamp_duty": 0.25,
        "sale_date": "2025-02-15", "sale_nav": 186.40, "sale_value": 5620.0, "stt": 0.0, "units": 30.150,
        "gain_type": "STCG", "gain": 1107.75, "ltcg": 0.0, "stcg": 1107.75, "ltcg_taxable": 0.0,
        "acquisition_value": 4512.25,
    },
    {
        "fy": "2024-25", "scheme": "ICICI Prudential Liquid Fund - Direct Plan - Growth", "folio": "556677 / 88",
        "isin": "INF109K01VQ1", "fund_type": "DEBT",
        "purchase_date": "2023-05-10", "purchase_nav": 330.50, "purchase_value": 14972.0, "stamp_duty": 0.0,
        "sale_date": "2024-11-20", "sale_nav": 352.70, "sale_value": 15980.0, "stt": 0.02, "units": 45.300,
        "gain_type": "STCG", "gain": 1008.0, "ltcg": 0.0, "stcg": 1008.0, "ltcg_taxable": 0.0,
        "acquisition_value": 14972.0,
    },
    {
        "fy": "2024-25", "scheme": "SBI Small Cap Fund - Direct Plan - Growth", "folio": "445566778",
        "isin": "INF200K01T51", "fund_type": "EQUITY",
        "purchase_date": "2023-07-01", "purchase_nav": 131.30, "purchase_value": 7476.0, "stamp_duty": 0.0,
        "sale_date": "2025-01-10", "sale_nav": 148.50, "sale_value": 7100.0, "stt": 0.0, "units": 47.800,
        "gain_type": "LTCG", "gain": -376.0, "ltcg": -376.0, "stcg": 0.0, "ltcg_taxable": -376.0,
        "acquisition_value": 7476.0,
    },
]

SAMPLE_DEMAT = {
    "statement_period": {"from": "2024-11-01", "to": "2024-11-30"},
    "file_type": "NSDL",
    "investor_info": {
        "name": "Priya Menon",
        "email": "priya.menon@example.com",
        "address": "44, Marine Drive, Mumbai 400020",
        "mobile": "+9198XXXXXX10",
    },
    "parse_warnings": [],
    "nps": None,
    "accounts": [{
        "name": "HDFC Securities Ltd", "type": "NSDL", "dp_id": "IN300476", "client_id": "10234567",
        "folios": 1, "balance": 259013,
        "owners": [{"name": "Priya Menon", "PAN": "AAAPM1234K"}],
        "equities": [
            {"isin": "INE002A01018", "name": "Reliance Industries Ltd", "num_shares": 25, "price": 2890, "value": 72250, "symbol": "RELIANCE", "exchange": "NSE"},
            {"isin": "INE009A01021", "name": "Infosys Ltd", "num_shares": 40, "price": 1560, "value": 62400, "symbol": "INFY", "exchange": "NSE"},
            {"isin": "INE155A01022", "name": "Tata Motors Ltd", "num_shares": 60, "price": 780, "value": 46800, "symbol": "TATAMOTORS", "exchange": "NSE"},
        ],
        "mutual_funds": [
            {"isin": "INF879O01027", "name": "Parag Parikh Flexi Cap Fund - Direct Growth", "amfi": "122639", "type": "EQUITY", "balance": 320.5, "nav": 78.20, "value": 25063, "avg_cost": 62.40, "total_cost": 19999, "ucc": None, "folio": "PPFAS/1122", "pnl": 5064, "return": 25.3},
        ],
        "bonds": [
            {"isin": "INE0BGX07018", "name": "8.15% GOI 2032", "num_bonds": 50, "value": 52500, "face_value": 1000, "coupon_rate": 8.15, "coupon_frequency": "Semi-annual", "maturity_date": "2032-06-15", "market_price": 1050},
        ],
    }],
}

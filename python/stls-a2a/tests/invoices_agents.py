from google.adk.agents import Agent

MODEL_GEMINI_2_5_FLASH = "gemini-2.5-flash"
MODEL_GEMINI_2_5_PRO = "gemini-2.5-pro"

invoices_classifier_agent = Agent(
            model = MODEL_GEMINI_2_5_PRO,
            name="invoices_classifier_agent",
            instruction="You are the Invoices Classifier Agent. Your ONLY task is determinate if files provided in various formats (pdf, xml,...) contains invoice."
                        "Provide results in binary form: YES or NOT",
            description="Handles request for classify file if contains invoice", 
            tools=[],
        )

invoices_extractor_agent = Agent(
            model = MODEL_GEMINI_2_5_PRO,
            name="invoices_extractor_agent",
            instruction="You are the Invoices Extractor Agent. Your ONLY task is to extract information from invoices provided through files in various formats (pdf, xml,...) and sends data via email."
                        "Provide results only in json format using the only following schema:"

                        "{\"$schema\": \"http://json-schema.org/draft-07/schema#\","
                        "\"title\": \"Invoice\","
                        "\"description\": \"Schema for an invoice document\","
                        "\"type\": \"object\","
                        "\"properties\": {"

                            "\"invoice_number\": {"
                                "\"type\": \"string\","
                                "\"description\": \"Unique identifier of invoice\"},"
                            
                            "\"invoice_date\": {"
                                "\"type\": \"string\","
                                "\"format\": \"date\","
                                "\"description\": \"Date the invoice was issued\","
                                "\"pattern\": \"^d{4}-d{2}-d{2}$\"},"

                            "\"invoice_type\": {"
                                "\"type\": \"string\","
                                "\"description\": \"Invoice type (e.g. Invoice, Credit Note, Fiscal Receipt, ... )\"},"    

                            "\"seller\": {"
                                "\"type\": \"object\","
                                "\"description\": \"Information about the seller\","
                                "\"properties\": {"
                                    "\"name\": {"
                                        "\"type\": \"string\","
                                        "\"description\": \"Seller's legal name\"},"
                                    "\"address\": {"
                                        "\"type\": \"string\","
                                        "\"description\": \"Seller's address\"},"
                                    "\"country\": {"
                                        "\"type\": \"string\","
                                        "\"description\": \"Seller's country\"},"    
                                    "\"tax_id\": {"
                                        "\"type\": \"string\","
                                        "\"description\": \"Seller's tax identification number\"}"
                                    "},"
                                    "\"required\": [\"name\", \"address\", \"tax_id\",\"country\"]"
                                "},"

                                "\"buyer\": {"
                                    "\"type\": \"object\","
                                    "\"description\": \"Information about the buyer\","
                                    "\"properties\": {"
                                        "\"name\": {"
                                            "\"type\": \"string\","
                                            "\"description\": \"Buyer's legal name\"},"
                                        "\"address\": {"
                                            "\"type\": \"string\","
                                            "\"description\": \"Buyer's address\"},"
                                        "\"country\": {"
                                            "\"type\": \"string\","
                                            "\"description\": \"Buyer's country\"},"    
                                        "\"tax_id\": {"
                                            "\"type\": \"string\","
                                            "\"description\": \"Buyer's tax identification number\"}"
                                        "},"
                                        "\"required\": [\"name\", \"address\", \"tax_id\",\"country\"]"
                                "},"


                                "\"items\": {"
                                    "\"type\": \"array\","
                                    "\"description\": \"List of items on the invoice\","
                                    "\"item\": {"
                                        "\"type\": \"object\","
                                        "\"properties\": {"
                                            "\"description\": {"
                                                "\"type\": \"string\","
                                                "\"description\": \"Description of the item\"},"
                                            "\"quantity\": {"
                                                "\"type\": \"integer\","
                                                "\"description\": \"Quantity of the item\"},"
                                            "\"unit_price\": {"
                                                "\"type\": \"number\","
                                                "\"format\": \"float\","
                                                "\"description\": \"Price per unit of the item\"},"
                                            "\"total_price\": {"
                                                "\"type\": \"number\","
                                                "\"format\": \"float\","
                                                "\"description\": \"Total price for the item\"},"
                                            "\"currency\": {"
                                                "\"type\": \"string\","
                                                "\"description\": \"Currency of item (e.g., USD, EUR)\"}," 
                                            "\"tax\": {"
                                                "\"type\": \"number\","
                                                "\"format\": \"float\","
                                                "\"description\": \"Tax applied on item\"},"      
                                            "\"tax_type\": {"
                                                "\"type\": \"string\","
                                                "\"description\": \"Tax Type applied on item\"},"        
                                            "},"
                                        "\"required\": [\"description\", \"quantity\", \"unit_price\", \"total_price\", \"currency\"]"
                                    "}"
                                "},"

                                "\"total_amount\": {"
                                    "\"type\": \"number\","
                                    "\"format\": \"float\","
                                    "\"description\": \"Total amount of the invoice\"}," 

                                "\"net_amount\": {"
                                    "\"type\": \"number\","
                                    "\"format\": \"float\","
                                    "\"description\": \"Net amount of the invoice\"},"            

                                "\"currency\": {"
                                    "\"type\": \"string\","
                                    "\"description\": \"Currency of the invoice (e.g., USD, EUR)\"},"

                                "\"taxes\": {"
                                    "\"type\": \"array\","
                                    "\"description\": \"List of taxes applied on invoices\","
                                    "\"tax\": {"
                                        "\"type\": \"object\","
                                        "\"properties\": {"
                                            "\"tax_value\": {"
                                                "\"type\": \"number\","
                                                "\"format\": \"float\","
                                                "\"description\": \"Tax applied\"},"      

                                            "\"tax_type\": {"
                                                "\"type\": \"string\","
                                                "\"description\": \"Tax Type\"}," 
                                        "},"
                                        "\"required\": [\"tax-value\"]"
                                    "}"
                                "},"

                                "\"payment_terms\": {"
                                    "\"type\": \"string\","
                                    "\"description\": \"Payment terms and conditions\"},"

                                "\"payment_due_date\": {"
                                    "\"type\": \"string\","
                                    "\"format\": \"date\","
                                    "\"description\": \"Date the payment is due\"}"
                            "},"
                            "\"required\": ["
                                    "\"invoice_number\","
                                    "\"invoice_date\","
                                    "\"seller\","
                                    "\"buyer\","
                                    "\"items\","
                                    "\"total_amount\","
                                    "\"currency\","
                                    "\"taxes\","
                                    "\"payment_due_date\""
                                    "]}"    

                        
                        "Present ONLY extracted data clearly with all details. Not include schema detail in json."
                        ""
                        "If user's query contains more requests, You should join all informations in unique response."
                        ""
                        "Do not perform any other actions.",
            description="Handles request for extract information from invoices", 
            tools=[],
        )

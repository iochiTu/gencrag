-- Fictional sample catalog for local testing (prices in USD). Replace with your real data
-- (or point DATABASE_URL at your existing database and map your tables to this schema).

TRUNCATE recommendation_log, business_rules, product_ingredients, product_attributes,
         products, ingredients, skin_concern_map, app_settings, admin_audit, import_jobs,
         chat_messages, chat_conversations, analytics_events RESTART IDENTITY CASCADE;

INSERT INTO skin_concern_map (concern, label, helpful_ingredients) VALUES
 ('acne',         'Breakouts',         '{Salicylic Acid,Centella Asiatica Extract,Houttuynia Cordata Extract,Zinc PCA,Tea Tree Leaf Oil}'),
 ('dryness',      'Dryness',           '{Sodium Hyaluronate,Ceramide NP,Glycerin,Squalane,Panthenol}'),
 ('redness',      'Redness',           '{Centella Asiatica Extract,Madecassoside,Panthenol,Allantoin,Zinc Oxide}'),
 ('pigmentation', 'Dark spots',        '{Niacinamide,Arbutin,Rice Ferment Filtrate}'),
 ('wrinkles',     'Fine lines',        '{Retinol,Adenosine,Palmitoyl Tripeptide-1,Ginseng Root Extract}'),
 ('pores',        'Enlarged pores',    '{Salicylic Acid,Niacinamide,Zinc PCA,Glycolic Acid}'),
 ('dullness',     'Dullness',          '{Niacinamide,Glycolic Acid,Rice Ferment Filtrate,Propolis Extract}'),
 ('oiliness',     'Excess oil',        '{Zinc PCA,Green Tea Extract,Niacinamide}');

INSERT INTO ingredients (inci_name, function, flags) VALUES
 ('Glycerin',                   'humectant',     '{}'),
 ('Centella Asiatica Extract',  'soothing',      '{}'),
 ('Panthenol',                  'soothing',      '{}'),
 ('Salicylic Acid',             'exfoliant',     '{bha}'),
 ('Tea Tree Leaf Oil',          'anti-blemish',  '{essential_oil}'),
 ('Zinc PCA',                   'sebum control', '{}'),
 ('Rice Ferment Filtrate',      'brightening',   '{}'),
 ('Niacinamide',                'brightening',   '{}'),
 ('Fragrance',                  'fragrance',     '{fragrance}'),
 ('Houttuynia Cordata Extract', 'soothing',      '{}'),
 ('Allantoin',                  'soothing',      '{}'),
 ('Glycolic Acid',              'exfoliant',     '{aha}'),
 ('Lactic Acid',                'exfoliant',     '{aha}'),
 ('Alcohol Denat.',             'solvent',       '{alcohol}'),
 ('Sodium Hyaluronate',         'humectant',     '{}'),
 ('Ceramide NP',                'barrier',       '{}'),
 ('Arbutin',                    'brightening',   '{}'),
 ('Retinol',                    'anti-aging',    '{retinoid}'),
 ('Squalane',                   'emollient',     '{}'),
 ('Madecassoside',              'soothing',      '{}'),
 ('Shea Butter',                'emollient',     '{}'),
 ('Green Tea Extract',          'antioxidant',   '{}'),
 ('Ginseng Root Extract',       'anti-aging',    '{}'),
 ('Adenosine',                  'anti-wrinkle',  '{}'),
 ('Palmitoyl Tripeptide-1',     'anti-aging',    '{}'),
 ('Zinc Oxide',                 'UV filter',     '{}'),
 ('Titanium Dioxide',           'UV filter',     '{}'),
 ('Propolis Extract',           'nourishing',    '{}'),
 ('Honey Extract',              'nourishing',    '{}');

INSERT INTO products (sku, name, brand, category, routine_step, price, cost, stock_qty, description) VALUES
 ('CL-001','Low-pH Gentle Foam Cleanser','Sample','cleanser',1,12.00,3.60,120,
  'pH 5.5 foam cleanser with centella and panthenol. Cleans without stripping the barrier or leaving skin tight; suits dry and sensitive skin.'),
 ('CL-002','BHA Pore Cleansing Gel','Sample','cleanser',1,14.00,3.50,80,
  'Gel cleanser with salicylic acid and tea tree oil that clears excess sebum and blackheads. For oily, acne-prone skin and clogged pores.'),
 ('TN-001','Rice Brightening Toner','Sample','toner',2,19.00,4.75,60,
  'Rice ferment filtrate and niacinamide brighten a dull, uneven skin tone and fade spots.'),
 ('TN-002','Houttuynia Calming Toner','Sample','toner',2,16.00,4.00,90,
  '77% houttuynia cordata extract calms breakouts and redness. Fragrance free.'),
 ('TN-003','AHA Peeling Toner','Sample','toner',2,21.00,4.40,40,
  'Glycolic and lactic acid exfoliate dead skin cells to smooth rough texture and refine enlarged pores.'),
 ('SR-001','Hyaluronic Hydra Ampoule','Sample','serum',3,28.00,6.10,70,
  'Low-molecular hyaluronic acid and ceramides restore deep hydration for dry and dehydrated skin.'),
 ('SR-002','Niacinamide 10% Spot Serum','Sample','serum',3,25.00,5.00,100,
  '10% niacinamide with arbutin fades dark spots and post-blemish marks and refines pores.'),
 ('SR-003','Retinol 0.3% Repair Serum','Sample','serum',3,39.00,7.80,35,
  'Night serum with 0.3% retinol and squalane to soften fine lines and improve firmness. Not for use during pregnancy.'),
 ('SR-004','Cica Repair Ampoule','Sample','serum',3,24.00,7.20,150,
  'Centella and madecassoside soothe irritated, red and breakout-prone skin.'),
 ('CR-001','Ceramide Barrier Cream','Sample','cream',5,30.00,8.40,85,
  'Rich cream with ceramides and shea butter that strengthens a damaged skin barrier. For very dry, sensitive skin.'),
 ('CR-002','Green Tea Water Gel Cream','Sample','cream',5,23.00,4.60,110,
  'Oil-free gel cream with green tea and hyaluronic acid. Hydrates without shine for oily and combination skin.'),
 ('CR-003','Ginseng Firming Cream','Sample','cream',5,52.00,11.50,25,
  'Premium cream with ginseng, peptides and adenosine for fine lines, loss of firmness and dullness.'),
 ('SS-001','Mineral Mild Sunscreen SPF50+','Sample','sunscreen',6,18.00,5.40,95,
  'Zinc oxide mineral sunscreen that protects sensitive and redness-prone skin without stinging.'),
 ('SS-002','Fresh Aqua Sun Essence SPF50+','Sample','sunscreen',6,20.00,4.00,130,
  'Lightweight, non-greasy sun essence with niacinamide for oily skin.'),
 ('MK-001','Propolis Soothing Sheet Mask','Sample','mask',7,3.00,0.60,500,
  'Propolis and honey extract nourish tired, dull, dry skin and add glow.');

INSERT INTO product_attributes (sku, suitable_skin_types, target_concerns, avoid_for, texture, fragrance_free) VALUES
 ('CL-001','{dry,normal,combination,sensitive}','{dryness,redness}','{}','foam',TRUE),
 ('CL-002','{oily,combination}','{acne,pores,oiliness}','{}','gel',FALSE),
 ('TN-001','{normal,dry,combination}','{dullness,pigmentation}','{}','watery',FALSE),
 ('TN-002','{oily,combination,sensitive,normal}','{acne,redness}','{}','watery',TRUE),
 ('TN-003','{normal,oily,combination}','{dullness,pores}','{}','watery',TRUE),
 ('SR-001','{dry,normal,combination,sensitive}','{dryness}','{}','light',TRUE),
 ('SR-002','{normal,oily,combination}','{pigmentation,pores,dullness}','{}','light',TRUE),
 ('SR-003','{normal,dry,combination}','{wrinkles}','{pregnancy}','light',TRUE),
 ('SR-004','{sensitive,dry,normal,combination,oily}','{redness,acne}','{}','light',TRUE),
 ('CR-001','{dry,normal,sensitive}','{dryness,redness}','{}','rich',TRUE),
 ('CR-002','{oily,combination,normal}','{oiliness,dryness}','{}','gel',FALSE),
 ('CR-003','{dry,normal}','{wrinkles,dullness}','{}','rich',FALSE),
 ('SS-001','{sensitive,dry,normal,combination}','{redness}','{}','cream',TRUE),
 ('SS-002','{oily,combination,normal}','{oiliness,pigmentation}','{}','essence',TRUE),
 ('MK-001','{dry,normal,combination,oily}','{dryness,dullness}','{}','sheet',TRUE);

-- product -> ingredients, in INCI order
INSERT INTO product_ingredients (sku, ingredient_id, position)
SELECT x.sku, i.id, x.pos
FROM (VALUES
 ('CL-001','Glycerin',1),('CL-001','Centella Asiatica Extract',2),('CL-001','Panthenol',3),
 ('CL-002','Salicylic Acid',1),('CL-002','Zinc PCA',2),('CL-002','Tea Tree Leaf Oil',3),
 ('TN-001','Rice Ferment Filtrate',1),('TN-001','Niacinamide',2),('TN-001','Fragrance',3),
 ('TN-002','Houttuynia Cordata Extract',1),('TN-002','Panthenol',2),('TN-002','Allantoin',3),
 ('TN-003','Glycolic Acid',1),('TN-003','Lactic Acid',2),('TN-003','Alcohol Denat.',3),
 ('SR-001','Sodium Hyaluronate',1),('SR-001','Ceramide NP',2),('SR-001','Panthenol',3),
 ('SR-002','Niacinamide',1),('SR-002','Arbutin',2),('SR-002','Zinc PCA',3),
 ('SR-003','Retinol',1),('SR-003','Squalane',2),('SR-003','Ceramide NP',3),
 ('SR-004','Centella Asiatica Extract',1),('SR-004','Madecassoside',2),('SR-004','Panthenol',3),
 ('CR-001','Ceramide NP',1),('CR-001','Shea Butter',2),('CR-001','Squalane',3),
 ('CR-002','Green Tea Extract',1),('CR-002','Sodium Hyaluronate',2),('CR-002','Fragrance',3),
 ('CR-003','Ginseng Root Extract',1),('CR-003','Adenosine',2),('CR-003','Palmitoyl Tripeptide-1',3),('CR-003','Fragrance',4),
 ('SS-001','Zinc Oxide',1),('SS-001','Titanium Dioxide',2),('SS-001','Centella Asiatica Extract',3),
 ('SS-002','Niacinamide',1),('SS-002','Sodium Hyaluronate',2),('SS-002','Alcohol Denat.',3),
 ('MK-001','Propolis Extract',1),('MK-001','Honey Extract',2),('MK-001','Glycerin',3)
) AS x(sku, inci, pos)
JOIN ingredients i ON i.inci_name = x.inci;

-- Commercial levers (fictional): new launch, campaign, low stock
INSERT INTO business_rules (sku, priority, campaign_boost, valid_from, valid_to, updated_by) VALUES
 ('SR-004', 5, 0.00, NULL, NULL, 'merch'),                 -- new launch, push it
 ('CR-001', 4, 0.05, '2026-01-01', '2026-12-31', 'merch'), -- 2026 barrier campaign
 ('SS-002', 4, 0.00, NULL, NULL, 'merch'),
 ('CR-003', 2, 0.00, NULL, NULL, 'merch'),                 -- low stock, de-prioritise
 ('MK-001', 2, 0.00, NULL, NULL, 'merch');

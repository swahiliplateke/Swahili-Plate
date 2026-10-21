import re
from pathlib import Path

src_path = Path("c:/Users/Amina/Desktop/swahili plate web/Assets( Swahili plate)/Swahili_Plate_Food_Catalog_for_Copilot (1).md")
text = src_path.read_text(encoding='utf-8')
start = text.index('## Catalogue')
rows = []
for line in text[start:].splitlines():
    if line.startswith('|'):
        parts = [p.strip() for p in line.strip('|').split('|')]
        if len(parts) >= 4 and parts[0] not in {'Category', '---'} and not parts[0].startswith('---'):
            rows.append(parts)

items = []
for parts in rows:
    if len(parts) < 4:
        continue
    category, name, price, desc = parts[0], parts[1], parts[2], parts[3]
    if price.startswith('Price') or price == '---':
        continue
    price = price.replace(',', '').replace(' ', '')
    if price and price[0].isdigit():
        try:
            val = int(float(price))
        except ValueError:
            continue
        items.append((category, name, val, desc))

seen = []
for cat, *_ in items:
    if cat not in seen:
        seen.append(cat)

cat_to_page = {
    'Breakfast': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101414_0000%20(1).png',
    'Breakfast Combos': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101414_0000%20(1).png',
    'Light Breakfast': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101414_0000%20(1).png',
    'Snacks': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Sandwiches': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Wraps / Shawarma': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Vegetables': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Soup': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Salads': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Starters': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Wings / Starters': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Starters / Platters': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101606_0000%20(1).png',
    'Chicken Dishes': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
    'Pasta': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
    'Beef / Mutton': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
    'Fish / Seafood': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
    'Special Dishes': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
    'Extra Toppings': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
    'Platters': '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',
}

food_rows = []
for category, name, price, desc in items:
    image = cat_to_page.get(category, '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png')
    food_rows.append(f"        {{category: {category!r}, name: {name!r}, originalName: {name!r}, price: {price}, description: {desc!r}, image: {image!r}}},")

categories_text = ',\n  '.join(repr(cat) for cat in seen)
source_page_map = ',\n'.join(f"    {cat!r}: {url!r}" for cat, url in cat_to_page.items())
product_map = '\n'.join(
    f"  {name!r}: {cat_to_page.get(category, '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png')!r},"
    for category, name, _, _ in items
    if name in {'Spanish Omelette', 'Rolex', 'Chicken Biryani', 'Fish Platter', 'Mutton Mandhi', 'Beef Biryani', 'Arosto', 'Fruit Salad Plate'}
)

js = (
    "const MENU_CATEGORY_TAXONOMY = [\n"
    f"  {categories_text}\n"
    + "];\n\n"
    + "const MENU_IMAGE_AUDIT = {\n"
    + "  sourcePageMap: {\n"
    + source_page_map
    + "\n  },\n"
    + "  notes: 'The workspace contains menu-page PNG exports rather than individual product photos; category-level mapping is used where no direct product image exists.',\n"
    + "  hasOriginalPdf: false\n};\n\n"
    + "const PRODUCT_IMAGE_MAP = {\n"
    + "  default: '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Menu.pdf_20260913_101737_0000.png',\n"
    + product_map
    + "\n};\n\n"
    + "let MENU_DATA_CONFIG = {\n"
    + "  food: [\n"
    + "\n".join(food_rows)
    + "\n  ],\n"
    + "  drinks: [\n"
    + "    {category: 'Hot Drinks / Coffee', name: 'Espresso', originalName: 'Espresso', price: 200, description: 'Classic espresso.', image: '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Drinks%20Menu.pdf_20260917_082333_0000.png'},\n"
    + "    {category: 'Tea', name: 'Kenyan Tea', originalName: 'Kenyan tea', price: 150, description: 'Kenyan tea.', image: '/brand-assets/Swahili%20plate.%20assets/Swahili%20Plate%20Drinks%20Menu.pdf_20260917_082333_0000.png'}\n"
    + "  ]\n};\n"
)

print(js)

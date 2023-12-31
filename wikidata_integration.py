#!/usr/bin/env python

import re
from collections import defaultdict
import jsonlines
import argparse
import json

import psycopg2
from psycopg2 import extras

DATE_PARSE_RE = re.compile(r'(\d+)-(\d+)-(\d+)T(\d+):(\d+):(\d+)')

def setup_db(connection_string):
    conn = psycopg2.connect(connection_string)
    cursor = conn.cursor()
    cursor.execute('DROP TABLE IF EXISTS wikidata')
    cursor.execute(
        'CREATE TABLE wikidata ('
        '    wikipedia_id TEXT PRIMARY KEY,'
        '    title TEXT,'
        '    wikidata_id TEXT,'
        '    description TEXT,'
        '    properties JSONB'
        ')'
    )
    cursor.execute('CREATE INDEX wikidata_wikidata_id ON wikidata(wikidata_id)')
    cursor.execute('CREATE INDEX wikidata_properties ON wikidata USING gin(properties)')
    return conn, cursor

def map_value(value, id_name_map):
    if not value or not 'type' in value or not 'value' in value:
        return None
    typ = value['type']
    value = value['value']
    if typ == 'string':
        return value
    elif typ == 'wikibase-entityid':
        entity_id = value['id']
        return id_name_map.get(entity_id)
    elif typ == 'time':
        time_split = DATE_PARSE_RE.match(value['time'])
        if not time_split:
            return None
        year, month, day, hour, minute, second = map(int, time_split.groups())
        if day == 0:
            day = 1
        if month == 0:
            month = 1
        return '%04d-%02d-%02dT%02d:%02d:%02d' % (year, month, day, hour, minute, second)
    elif typ == 'quantity':
        return float(value['amount'])
    elif typ == 'monolingualtext':
        return value['text']
    elif typ == 'globecoordinate':
        lat = value.get('latitude')
        lng = value.get('longitude')
        if lat or lng:
            res = {'lat': lat, 'lng': lng}
            globe = value.get('globe', '').rsplit('/', 1)[-1]
            if globe != 'Q2' and globe in id_name_map:
                res['globe'] = globe
            if value.get('altitude'):
                res['altitude'] = value['altitude']
            return res
    return None

def process_chunk(chunk, cursor, id_name_map):
    c = 0
    skip = 0

    for d in chunk:
        c += 1
        if c % 1000 == 0:
            print(c, skip)

        try:
            wikipedia_id = d.get('sitelinks', {}).get('enwiki', {}).get('title')
            title = d['labels'].get('en', {}).get('value')
            description = d['descriptions'].get('en', {}).get('value')
            wikidata_id = d['id']
            properties = {}

            if wikipedia_id and title:
                if wikipedia_id in id_name_map:
                    skip += 1
                    continue
                id_name_map[wikipedia_id] = title

                for prop_id, claims in d['claims'].items():
                    prop_name = id_name_map.get(prop_id)
                    if prop_name:
                        ranks = defaultdict(list)
                        for claim in claims:
                            mainsnak = claim.get('mainsnak')
                            if mainsnak:
                                data_value = map_value(mainsnak.get('datavalue'), id_name_map)
                                if data_value:
                                    lst = ranks[claim['rank']]
                                    if mainsnak['datavalue'].get('type') != 'wikibase-entityid':
                                        del lst[:]
                                    lst.append(data_value)

                        for r in 'preferred', 'normal', 'deprecated':
                            value = ranks[r]
                            if value:
                                if len(value) == 1:
                                    value = value[0]
                                else:
                                    value = sorted(value)
                                properties[prop_name] = value
                                break

                cursor.execute(
                    'INSERT INTO wikidata (wikipedia_id, title, wikidata_id, description, properties) VALUES (%s, %s, %s, %s, %s)',
                    (wikipedia_id, title, wikidata_id, description, extras.Json(properties)),
                )
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")

    print(f"Processed {c} entries, skipped {skip} entries.")

def main(args, cursor):
    id_name_map = {}

    try:
        with jsonlines.open(args.dump) as json_file:
            chunk_size = 1000  # Adjust the chunk size as needed
            chunk = []

            for line_number, line in enumerate(json_file.iter(), start=1):
                try:
                    entry = json.loads(line)
                    chunk.append(entry)
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON at line {line_number}: {e}")

                if len(chunk) >= chunk_size:
                    process_chunk(chunk, cursor, id_name_map)
                    chunk = []

            # Process the remaining lines in the last chunk
            if chunk:
                process_chunk(chunk, cursor, id_name_map)

    except jsonlines.jsonlines.InvalidLineError as e:
        print(f"Error reading JSON line: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import wikidata into postgres')
    parser.add_argument('--postgres', type=str, help='Postgres connection string')
    parser.add_argument('--dump', type=str, help='Path to JSON dump file')

    args = parser.parse_args()
    conn, cursor = setup_db(args.postgres)

    main(args, cursor)

    conn.commit()

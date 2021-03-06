# Copyright 2020 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import os
import ibm_boto3
import pandas as pd
from czml import CZMLBuilder
from ibm_botocore.client import Config
from flask import Flask, request, jsonify, render_template
from conjunction_search import get_nns_for_object, build_kd_forest

app = Flask(__name__)
DEV = bool(os.environ.get('DEV', False))


def get_dataframe_from_cos():
    """Fetches the orbital prediction pandas DataFrame
    from COS.

    :return: The DataFrame containing the orbital predictions for every
             RSO
    :rtype: pandas.DataFrame
    """
    cos_endpoint = os.environ.get('COS_ENDPOINT')
    cos_api_key_id = os.environ.get('COS_API_KEY_ID')
    cos_instance_crn = os.environ.get('COS_INSTANCE_CRN')
    cos_client = ibm_boto3.resource('s3',
                                    ibm_api_key_id=cos_api_key_id,
                                    ibm_service_instance_id=cos_instance_crn,
                                    config=Config(signature_version='oauth'),
                                    endpoint_url=cos_endpoint)
    cos_bucket = os.environ.get('COS_BUCKET')
    cos_filename = os.environ.get('COS_FILENAME')
    df_obj = cos_client.Object(cos_bucket, cos_filename).get()
    df = pd.read_pickle(io.BytesIO(df_obj['Body'].read()))
    return df

# Load test data if the env variable DEV is set to True
if DEV:
    orbit_df = pd.read_pickle('../sample_data/orbit_preds.pickle')
else:
    orbit_df = get_dataframe_from_cos()

# Build the KD-trees for each prediction timestep
kd_forest = build_kd_forest(orbit_df)


def _make_json_resp(conj_results):
    """Converts the conjunction search results into a
    JSON response payload.

    :param conj_results: The conjunction search results
    :type conj_results: [(int, int, float)]

    :return: A JSON list of the search results where each dictionary
             contains the UUID of the matching RSO, the distance to
             the query RSO, and the prediction timestep where the query
             matches.
    """
    keys = ('rso_id', 'timestep', 'distance')
    json_results = [dict(zip(keys, r)) for r in conj_results]
    return jsonify(json_results)


def _make_czml_resp(rso_id, conj_results):
    """Converts the conjunction search results to a CZML
    document to be used to display the orbits of the
    RSOs in Cesium

    :param rso_id: The ID of the RSO that the conjunction search
                   query was run for.
    :type rso_id: str

    :param conj_results: The conjunction search results
    :type conj_results: [(int, int, float)]

    return: A JSON CZML document describing the orbits of the
            conjunction search RSOs.
    """
    # Build a pandas DataFrame out of the conjunction search results
    conj_df = pd.DataFrame(data=conj_results,
                           columns=['rso_id', 'conj_timesteps', 'conj_dists'])
    # Gather conjunction timesteps and distances by rso_id
    conj_df = conj_df.pivot_table(index=['rso_id'],
                                  aggfunc=lambda x: list(x))
    conj_df.reset_index(inplace=True)
    if conj_df.empty:
        return jsonify([])
    # Select the query RSO and the conjunction search matches
    # from the orbital prediction DataFrame
    rso_ids = [rso_id] + [r[0] for r in conj_results]
    rsos_df = orbit_df[orbit_df.rso_id.isin(rso_ids)].reset_index()
    # Join the conjunction search and selected orbital prediction
    # DataFrames into one DataFrame
    czml_df = rsos_df.merge(conj_df, how='left', on='rso_id')
    # Build and return the CZML document
    czml_builder = CZMLBuilder(czml_df)
    czml_doc = czml_builder.build_czml_doc()
    return jsonify(czml_doc)


@app.route('/', methods=['GET'])
def ui():
    rsos = orbit_df[['rso_name', 'rso_id']].to_dict(orient='records')
    return render_template('index.html', rsos=rsos)


@app.route('/conjunction_search/<rso_id>', methods=['GET'])
def conjunction_search(rso_id):
    """Flask route for querying the KD-trees to find nearest
    RSOs

    :param rso_id: The UUID of the RSO to find nearest neighbors
                   for.  This value should be the UUID in the `rso_id`
                   column of the `orbit_df` DataFrame.
    :type rso_id: str

    *Query Parameters*
    :param k: The number of nearest neighbor results to return
    :type k: int

    :param radius: Instead of returning the k nearest neighbors,
                   return all RSOs that are within the given radius
                   for each prediction timestep.
    :type radius: float

    """
    k = request.args.get('k', 1, type=int)
    search_radius = request.args.get('radius', type=float)
    results = get_nns_for_object(orbit_df,
                                 kd_forest,
                                 rso_id,
                                 k=k,
                                 radius=search_radius)
    if request.content_type == 'application/json':
        resp = _make_json_resp(results)
    else:
        resp = _make_czml_resp(rso_id, results)
    return resp


if __name__ == '__main__':
    app.run(debug=DEV, host='0.0.0.0', port=8080)

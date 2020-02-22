import os
import sys
import json
import requests
import redis
import pandas as pd
import numpy as np
from scipy import spatial
from gensim.models import word2vec
import pickle
import MeCab

from flask import Flask, request, abort
from flask_api import status
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,LocationMessage,FlexSendMessage,QuickReplyButton, LocationAction, QuickReply
)

app = Flask(__name__)

CHANNEL_SECRET = os.environ["CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ['CHANNEL_ACCESS_TOKEN']
STATION_API_URL = 'http://express.heartrails.com/api/json?method=getStations&x={}&y={}'

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


REDIS_URL = os.environ['REDIS_URL'] if os.environ.get(
    'REDIS_URL') != None else 'localhost:6379'

# コネクションプールから１つ取得
pool = redis.ConnectionPool.from_url(REDIS_URL, db=0)
# コネクションを利用
redis = redis.StrictRedis(connection_pool=pool)

mecab = MeCab.Tagger ('-Owakati')

f = open("./pickle/skip_list.txt","rb")
skip_list = pickle.load(f)
f.close()

vector_size = 250

model = word2vec.Word2Vec.load('./models/skip_w2v.model')


df = pd.read_csv('./csv/review_wakati.csv')
df = df.drop(['store_id'],axis=1)
df = df.rename(columns={'Unnamed: 0':'store_id'}).set_index('store_id')

@app.route("/hello")
def hello_world():
    return "hello world!",status.HTTP_200_OK

@app.route('/', methods=['POST'])
def index():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return '',status.HTTP_200_OK

@handler.add(MessageEvent, message=TextMessage)
def message_text(event):
    uid = event.source.user_id
    h = redis.hgetall(uid)
    token = event.reply_token

    if h:
        lat, long = h['lat'.encode()].decode(), h['long'.encode()].decode()
    else:
        quick_reply(token)
        return

    stations = get_stations(lat, long)
    print(stations)
    if  not stations:
        send_message(token, "エラーが発生しました。やり直して下さい。")
        return

    nouns = get_noun(event.message.text)
    vectors = avg_feature_vectors(nouns.split(' '), model)

    result = {}
    for i, row in df.iterrows():
        store_station = prepro_station(row.station)
        if store_station not in stations:
            continue

        score = sentence_similarity(vectors, skip_list[i])
        result[i] = score * row.score

    score_sorted = sorted(result.items(), key=lambda x:x[1], reverse=True)

    carousel = {
            "type": "flex",
            "altText": "おすすめはこちら",
            "contents": {
                "type": "carousel",
                "contents": []
            }
    }

    for t in score_sorted[:3]:
        row = df[df.index == t[0]]
        name, score, station = row.store_name.values[0], row.score.values[0], row.station.values[0]

        carousel['contents']['contents'].append(create_bubble(name, score, t[1]*100, 'https://www.google.com/search?'+name))

        # send_message(token, name)
        # break
        # print('店名:{}, 食べログスコア:{}, 独自スコア:{:.2f}, 最寄駅:{}'.format(name,score,t[1]*100,station))

    container_obj = FlexSendMessage.new_from_json_dict(carousel)

    send_message(token, container_obj)

@handler.add(MessageEvent, message=LocationMessage)
def message_location(event):
    lat = event.message.latitude
    long = event.message.longitude
    uid = event.source.user_id
    station = get_station(lat, long)
    token = event.reply_token

    if station:
        redis.hset(uid, 'lat', lat)
        redis.hset(uid, 'long', long)
        redis.expire(uid, 1800)
        message = '{}駅周辺のラーメン屋をお探しします！\nあなたの今の気分を教えて下さい\n（例）あっさりした醬油ラーメン'.format(station)
        send_message(token, message)
    else:
        send_message(token, "エラーが発生しました。やり直して下さい。")

def send_message(token, message):
    line_bot_api.reply_message(
        token,
        TextSendMessage(text=message)
    )

def create_bubble(name, score, original_score, uri):
    bubble = open("bubble.json","r")
    json_bubble = json.load(bubble)
    json_bubble['body']['contents'][0]['text'] = name
    json_bubble['body']['contents'][1]['contents'][0]['contents'][1]['text'] = score
    json_bubble['body']['contents'][1]['contents'][1]['contents'][1]['text'] = original_score
    json_bubble['footer']['contents'][0]['action']['uri'] = uri
    
    dumps_bubble = json.dumps(json_bubble)
    
    return json.loads(dumps_jaon)

def quick_reply(token):
    items = [QuickReplyButton(action=LocationAction(label='位置情報を送信する', text="位置情報を送信する"))]
    text = '位置情報を送信して下さい！\n最寄駅とその前後の駅を設定します。\n位置情報を送り直すことで再設定することができます！'

    messages = TextSendMessage(text=text, quick_reply=QuickReply(items=items))

    line_bot_api.reply_message(token, messages=messages)


def get_station(lat, long):
    station = http_request(STATION_API_URL.format(long, lat))
    if station:
        return station['response']['station'][0]['name']
    else:
        return ''

def get_stations(lat, long):
    stations = http_request(STATION_API_URL.format(long, lat))
    if stations:
        return [
            stations['response']['station'][0]['name'],
            stations['response']['station'][0]['prev'],
            stations['response']['station'][0]['next']
            ]
    else:
        return ''

def http_request(url):
    res = requests.get(url)
    
    if res.status_code != status.HTTP_200_OK:
        return ''

    result_json = res.json()
    return result_json

def prepro_station(station):
    if '（東武・都営・メトロ）' in station:
        return station.strip('（東武・都営・メトロ）').strip('駅')

    if '（つくばＥＸＰ）' in station:
        return station.strip('（つくばＥＸＰ）').strip('駅')

    if '（メトロ）' in station:
        return station.strip('（メトロ）').strip('駅')

    return station

def sentence_similarity(vec1, vec2):
    return 1 - spatial.distance.cosine(vec1, vec2)

def avg_feature_vectors(words, model):
    feature_vec = np.zeros(vector_size, dtype='float32')
    for word in words:
        try:
            feature_vec = np.add(feature_vec, model[word])
        except KeyError:
            pass

    return feature_vec

def get_noun(text):
    mecab.parse('')
    node = mecab.parseToNode(text)
    result = []

    while node:
        word = node.surface
        pos,pos2 = node.feature.split(",")[0],node.feature.split(",")[1]
        if '0' in word or '1' in word or '2' in word or '3' in word or '4' in word or '5' in word or '6' in word or '7' in word or '8' in word or '9' in word:
            node = node.next
            continue

        if pos2 == '数':
            node = node.next
            continue
            
        if (pos == '名詞' and pos2 != '数') or (pos == '形容詞') or (pos == '副詞'):
            result.append(word)
        node = node.next

    return ' '.join(result)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
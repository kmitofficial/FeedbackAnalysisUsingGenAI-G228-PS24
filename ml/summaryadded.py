# amazon webscrapped upto summary generation
from flask import Flask, request, jsonify
import torch
import torch.nn as nn
import joblib
import pandas as pd
from selenium import webdriver
from time import sleep
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import random
import warnings
import undetected_chromedriver as uc
from flask_cors import CORS
import spacy
from transformers import AutoTokenizer, AutoModel, pipeline
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from statistics import mode
import requests
from dotenv import load_dotenv
import os

load_dotenv()
app = Flask(__name__)
CORS(app)
warnings.filterwarnings("ignore", category=ResourceWarning)

nlp = spacy.load("en_core_web_sm")
tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
bert_model = AutoModel.from_pretrained("bert-base-uncased")

API_TOKEN = os.getenv("HF_API_TOKEN")
API_URL = os.getenv("API_URL")
headers = {"Authorization": f"Bearer {API_TOKEN}"}


def generate_review_summary(reviews):
    try:
        combined_reviews = " ".join(reviews)
        review_word_count = len(combined_reviews.split())
        max_length = min(150, review_word_count // 2)
        # max_length = 200
        # min_length = 100
        min_length = max(40, max_length // 2)

        if len(combined_reviews.split()) > 1024:
            combined_reviews = " ".join(combined_reviews.split()[:1024])
        payload = {
            "inputs": combined_reviews,
            "parameters": {
                "min_length": min_length,
                "max_length": max_length,
                "do_sample": False,
            },
        }
        response = requests.post(API_URL, headers=headers, json=payload)
        summary = response.json()[0]["summary_text"]
        if response.status_code == 200:
            return summary
        else:
            return "Unable to create summary"
    except Exception as e:
        print(f"Error generating summary: {str(e)}")
        return "Unable to create summary"


# summarizer = pipeline("summarization", model="facebook/bart-large-cnn")
# # summarizer = pipeline("summarization", model="sshleifer/distilbart-cnn-6-6")


# def generate_review_summary(reviews):
#     combined_reviews = " ".join(reviews)
#     review_word_count = len(combined_reviews.split())
#     max_length = min(150, review_word_count // 2)
#     min_length = max(40, max_length // 2)

#     if len(combined_reviews.split()) > 1024:
#         combined_reviews = " ".join(combined_reviews.split()[:1024])

#     summary = summarizer(
#         combined_reviews, max_length=max_length, min_length=min_length, do_sample=False
#     )[0]["summary_text"]
#     return summary


def extract_noun_phrases(text):
    doc = nlp(text)
    noun_phrases = []
    for chunk in doc.noun_chunks:
        noun_phrases.append(chunk.text)
    return noun_phrases


def get_bert_embedding(text):
    inputs = tokenizer(
        text, return_tensors="pt", padding=True, truncation=True, max_length=512
    )
    with torch.no_grad():
        outputs = bert_model(**inputs)
    return outputs.last_hidden_state.mean(dim=1).squeeze().numpy()


def extract_keyphrases(text, top_n=5):
    noun_phrases = extract_noun_phrases(text)
    if not noun_phrases:
        return []
    doc_embedding = get_bert_embedding(text)
    phrase_embeddings = [get_bert_embedding(phrase) for phrase in noun_phrases]
    similarities = cosine_similarity([doc_embedding], phrase_embeddings)[0]
    sorted_indices = np.argsort(similarities)[::-1]
    top_keyphrases = [noun_phrases[i] for i in sorted_indices[:top_n]]
    return top_keyphrases


class SentimentModel(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim):
        super(SentimentModel, self).__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, output_dim)
        self.relu = nn.ReLU()

    def forward(self, x):
        x = self.relu(self.fc1(x))
        x = self.fc2(x)
        return x


def load_model(
    model_path="sentiment_model.pth",
    tfidf_path="tfidf_vectorizer.pkl",
    label_encoder_path="label_encoder.pkl",
):
    input_dim = 4140
    hidden_dim = 128
    output_dim = 3
    model = SentimentModel(input_dim, hidden_dim, output_dim)
    model.load_state_dict(
        torch.load(model_path, map_location=torch.device("cpu"), weights_only=True)
    )
    model.eval()
    tfidf_vectorizer = joblib.load(tfidf_path)
    label_encoder = joblib.load(label_encoder_path)
    return model, tfidf_vectorizer, label_encoder


def setup_browser():
    options = uc.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
    return uc.Chrome(options=options)


def get_reviews_ratings(browser, url):
    reviews = {"review_text": [], "rating": [], "review_title": []}
    browser.get(url)
    sleep(3)

    try:
        for _ in range(3):
            review_elements = browser.find_elements(
                By.CSS_SELECTOR, 'span[data-hook="review-body"]'
            )
            rating_elements = browser.find_elements(By.CLASS_NAME, "review-rating")
            title_elements = browser.find_elements(
                By.CSS_SELECTOR, 'a[data-hook="review-title"]'
            )

            for review in review_elements:
                reviews["review_text"].append(review.text.strip())

            for rating in rating_elements:
                rating_text = rating.get_attribute("textContent").replace(
                    " out of 5 stars", ""
                )
                try:
                    reviews["rating"].append(float(rating_text))
                except ValueError:
                    reviews["rating"].append(None)

            for title in title_elements:
                reviews["review_title"].append(title.text.strip())

            # try:
            #     next_button = browser.find_element(By.XPATH, "//li[@class='a-last']/a")
            #     browser.execute_script("arguments[0].scrollIntoView();", next_button)
            #     sleep(2)
            #     next_button.click()
            #     sleep(3)
            # except:
            #     break

    except Exception as e:
        print(f"Scraping stopped: {e}")

    min_length = min(
        len(reviews["review_text"]),
        len(reviews["rating"]),
        len(reviews["review_title"]),
    )
    reviews["review_text"] = reviews["review_text"][:min_length]
    reviews["rating"] = reviews["rating"][:min_length]
    reviews["review_title"] = reviews["review_title"][:min_length]

    return reviews


def apply_sentiment_analysis(reviews_df, model, tfidf_vectorizer, label_encoder):
    review_features = tfidf_vectorizer.transform(reviews_df["review_text"])
    review_tensor = torch.tensor(review_features.toarray(), dtype=torch.float32)

    with torch.no_grad():
        predictions = model(review_tensor)
        _, predicted_labels = torch.max(predictions, 1)

    predicted_sentiments = label_encoder.inverse_transform(predicted_labels.numpy())
    reviews_df["predicted_sentiment"] = predicted_sentiments
    overall_sentiment = mode(predicted_sentiments)
    return reviews_df, overall_sentiment


model, tfidf_vectorizer, label_encoder = load_model()


# @app.route("/scrape", methods=["POST"])
# def scrape():
#     data = request.json
#     product_url = data.get("productUrl")

#     if not product_url:
#         return jsonify({"status": "error", "message": "No product URL provided"}), 400

#     try:
#         browser = setup_browser()
#         reviews = get_reviews_ratings(browser, product_url)
#         reviews_df = pd.DataFrame(reviews)

#         if reviews_df.empty:
#             return jsonify(
#                 {
#                     "status": "warning",
#                     "message": "No reviews found",
#                     "reviews": [],
#                     "total_reviews": 0,
#                     "overall_sentiment": None,
#                     "keyphrases": [],
#                     "summary": "",
#                 }
#             )

#         all_reviews_text = " ".join(reviews_df["review_text"].fillna("").tolist())
#         keyphrases = extract_keyphrases(all_reviews_text, top_n=10)
#         reviews_df["review_text"] = reviews_df["review_text"].fillna("")
#         reviews_df = reviews_df[reviews_df["review_text"].str.strip().str.len() > 0]

#         reviews_df, overall_sentiment = apply_sentiment_analysis(
#             reviews_df, model, tfidf_vectorizer, label_encoder
#         )
#         results = reviews_df.to_dict(orient="records")

#         review_summary = generate_review_summary(reviews_df["review_text"].tolist())

#         response_data = {
#             "status": "success",
#             "reviews": results,
#             "total_reviews": len(results),
#             "keyphrases": keyphrases,
#             "overall_sentiment": str(overall_sentiment),
#             "summary": review_summary,
#             "message": f"Successfully analyzed {len(results)} reviews",
#         }

#         return jsonify(response_data)

#     except Exception as e:
#         return (
#             jsonify(
#                 {
#                     "status": "error",
#                     "message": "Failed to process reviews. Please check the URL and try again.",
#                     "error_details": str(e),
#                     "overall_sentiment": None,
#                     "summary": "",
#                 }
#             ),
#             500,
#         )

#     finally:
#         if "browser" in locals():
#             browser.quit()


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.json
    product_url = data.get("productUrl")

    if not product_url:
        return jsonify({"status": "error", "message": "No product URL provided"}), 400

    try:
        browser = setup_browser()
        reviews = get_reviews_ratings(browser, product_url)
        reviews_df = pd.DataFrame(reviews)

        if reviews_df.empty:
            return jsonify(
                {
                    "status": "warning",
                    "message": "No reviews found",
                    "reviews": [],
                    "total_reviews": 0,
                    "overall_sentiment": None,
                    "keyphrases": [],
                    "summary": "",
                }
            )

        # Select top 12 reviews for keyphrase and summary generation
        top_n_reviews = reviews_df.head(7)
        all_reviews_text = " ".join(top_n_reviews["review_text"].fillna("").tolist())
        keyphrases = extract_keyphrases(all_reviews_text, top_n=10)

        reviews_df["review_text"] = reviews_df["review_text"].fillna("")
        reviews_df = reviews_df[reviews_df["review_text"].str.strip().str.len() > 0]

        reviews_df, overall_sentiment = apply_sentiment_analysis(
            reviews_df, model, tfidf_vectorizer, label_encoder
        )
        results = reviews_df.to_dict(orient="records")

        # Generate summary using only top 12 reviews
        review_summary = generate_review_summary(top_n_reviews["review_text"].tolist())

        response_data = {
            "status": "success",
            "reviews": results,
            "total_reviews": len(results),
            "keyphrases": keyphrases,
            "overall_sentiment": str(overall_sentiment),
            "summary": review_summary,
            "message": f"Successfully analyzed {len(results)} reviews",
        }

        return jsonify(response_data)

    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to process reviews. Please check the URL and try again.",
                    "error_details": str(e),
                    "overall_sentiment": None,
                    "summary": "",
                }
            ),
            500,
        )

    finally:
        if "browser" in locals():
            browser.quit()


if __name__ == "__main__":
    app.run(debug=True, port=5000)

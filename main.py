import json
from scraper import InDriveMentionScraper
from analyzer import MentionAnalyzer

def run_pipeline():
    print("🚀 Запуск AI-мониторинга inDrive...")
    
    # 1. Запускаем "Узел поиска" (как в n8n)
    scraper = InDriveMentionScraper()
    raw_articles = scraper.fetch_news() # Получаем сырые новости
    
    # 2. Запускаем "Узел анализа" (analyzer.py, который мы создали)
    analyzer = MentionAnalyzer()
    filtered_results = []

    print(f"🧐 Анализируем {len(raw_articles)} новостей...")

    for article in raw_articles:
        # Просим ИИ проанализировать конкретную новость
        analysis = analyzer.analyze_article(article['title'], article['text'])
        
        if analysis:
            # ЖЕСТКИЙ ФИЛЬТР: как ты делала для экспатов
            # Если релевантность ниже 8 — это "шлак", удаляем его
            if analysis.get('relevance_score', 0) >= 8:
                article['analysis'] = analysis
                filtered_results.append(article)
                print(f"✅ Найдено важное: {article['title']}")
            else:
                print(f"🗑 Пропущено (не релевантно): {article['title']}")

    # 3. Сохраняем только ЧИСТЫЙ результат
    with open('indrive_mentions_clean.json', 'w', encoding='utf-8') as f:
        json.dump(filtered_results, f, ensure_ascii=False, indent=4)
    
    print(f"🏁 Готово! Сохранено {len(filtered_results)} качественных новостей.")

if __name__ == "__main__":
    run_pipeline()
from bot.classifier import ANNOUNCEMENT, DIALOG, ClassifyInput, classify, learnable_words, surname_mentions
from bot.rules import Author, Rules


def verdict(text: str, **kw):
    return classify(ClassifyInput(text=text, **kw), Rules())


def test_examples_from_owner_are_announcements():
    examples = [
        "Внимание! Дежурим 4 дня: понедельник, вторник, среда, пятница. Приходить в заведение в 7:45, "
        "не позже. Пишите пожелания по дням недели.",
        "Иванов, Петрова, Сидоренко - сегодня прибыть в 9:00",
        "https://forms.yandex.ru/u/abc - пройти всем!",
    ]
    for text in examples:
        v = verdict(text)
        assert v.category == ANNOUNCEMENT, (text, v)


def test_small_talk_is_dialog():
    for text in ["спасибо", "ок", "Да, понял", "ахах", "а во сколько завтра?", "+"]:
        v = verdict(text)
        assert v.category == DIALOG, (text, v)


def test_reply_lowers_score():
    plain = verdict("сегодня в 9:00")
    reply = verdict("сегодня в 9:00", is_reply=True)
    assert reply.score < plain.score


def test_admin_raises_score():
    assert verdict("Завтра собрание", sender_is_admin=True).score > verdict("Завтра собрание").score


def test_always_forward_author():
    rules = Rules(always_forward_authors=[Author(42, "Начальник")])
    v = classify(ClassifyInput(text="ок", sender_id=42), rules)
    assert v.category == ANNOUNCEMENT


def test_participant_surnames_with_cases():
    surnames = {"иванов", "кузнецова", "ли"}
    assert surname_mentions("Иванову и Кузнецовой подойти", surnames) == 2
    # Короткие фамилии не ловим — слишком много ложных совпадений.
    assert surname_mentions("ли", surnames) == 0
    v = verdict("Иванову и Кузнецовой подойти", participant_surnames=surnames)
    assert any("фамилии участников" in r for r in v.reasons)


def test_keyword_prefix_and_dialog_whole_word():
    v = verdict("прибыли все")
    assert any("прибы" in r for r in v.reasons)
    # «да» как слово диалога не должно цеплять «дата».
    v = verdict("дата сдачи")
    assert not any("слова диалога" in r for r in v.reasons)


def test_weekday_abbreviation_needs_whole_word():
    assert not any("день недели" in r for r in verdict("сразу").reasons)
    assert any("день недели" in r for r in verdict("в ср").reasons)


def test_learnable_words_skip_known():
    words = learnable_words("Внимание, построение на плацу", Rules())
    assert "внимание" not in words
    assert "построение" not in words
    assert "плацу" in words

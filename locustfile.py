from locust import HttpUser, task, between


class SolaUser(HttpUser):
    wait_time = between(1, 3)  # Пауза между действиями 1-3 сек

    def on_start(self):
        """
        Этот метод запускается один раз при старте каждого 'юзера'.
        Здесь мы проходим авторизацию, чтобы получить куки.
        """
        response = self.client.post("/api/login", json={
            "email": "admin@healthclub.local",  # <-- ВСТАВЬ СЮДА РЕАЛЬНЫЙ EMAIL
            "password": "*963.*963."  # <-- ВСТАВЬ СЮДА РЕАЛЬНЫЙ ПАРОЛЬ
        })

        if response.status_code != 200:
            print(f"Login failed: {response.text}")

    @task(3)
    def view_profile(self):
        # Теперь, имея куки, мы можем смотреть профиль
        # Используем /profile, так как / может редиректить
        self.client.get("/profile")

    @task(1)
    def check_calendar(self):
        # Запрашиваем тренировки
        self.client.get("/api/trainings")

    @task(1)
    def check_meals(self):
        # Запрашиваем данные для графика (если есть такой эндпоинт)
        self.client.get("/api/user/weekly_summary")
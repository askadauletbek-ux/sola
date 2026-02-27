[33mcommit 14d602b7d1293493396e56433f6e39f10416d2b8[m[33m ([m[1;32mmain[m[33m)[m
Author: AskarDK <Dauletbekoffa@gmail.com>
Date:   Fri Feb 27 12:40:55 2026 +0500

    commit

[1mdiff --git a/.idea/workspace.xml b/.idea/workspace.xml[m
[1mindex c325e33..0507a97 100644[m
[1m--- a/.idea/workspace.xml[m
[1m+++ b/.idea/workspace.xml[m
[36m@@ -36,8 +36,6 @@[m
       <change beforePath="$PROJECT_DIR$/../SalesJourney/webhooks.py" beforeDir="false" afterPath="$PROJECT_DIR$/../SalesJourney/webhooks.py" afterDir="false" />[m
       <change beforePath="$PROJECT_DIR$/../SmartFood/backend/app.py" beforeDir="false" afterPath="$PROJECT_DIR$/../SmartFood/backend/app.py" afterDir="false" />[m
       <change beforePath="$PROJECT_DIR$/../SmartFood/backend/models.py" beforeDir="false" afterPath="$PROJECT_DIR$/../SmartFood/backend/models.py" afterDir="false" />[m
[31m-      <change beforePath="$PROJECT_DIR$/app.py" beforeDir="false" afterPath="$PROJECT_DIR$/app.py" afterDir="false" />[m
[31m-      <change beforePath="$PROJECT_DIR$/user_bp.py" beforeDir="false" afterPath="$PROJECT_DIR$/user_bp.py" afterDir="false" />[m
       <change beforePath="$PROJECT_DIR$/../foodstream/.env" beforeDir="false" afterPath="$PROJECT_DIR$/../foodstream/.env" afterDir="false" />[m
       <change beforePath="$PROJECT_DIR$/../foodstream/ai_kitchen.py" beforeDir="false" afterPath="$PROJECT_DIR$/../foodstream/ai_kitchen.py" afterDir="false" />[m
       <change beforePath="$PROJECT_DIR$/../foodstream/alembic.ini" beforeDir="false" afterPath="$PROJECT_DIR$/../foodstream/alembic.ini" afterDir="false" />[m
[1mdiff --git a/templates/admin_dashboard.html b/templates/admin_dashboard.html[m
[1mindex cad76c6..9dd7667 100644[m
[1m--- a/templates/admin_dashboard.html[m
[1m+++ b/templates/admin_dashboard.html[m
[36m@@ -39,7 +39,7 @@[m
     aiq: "{{ url_for('admin_ai_queue') }}",[m
     jobs: "{{ url_for('admin_jobs') }}",[m
     prompts: "{{ url_for('admin_prompts') }}",[m
[31m-    broadcast: "{{ url_for('admin_broadcast') }}",[m
[32m+[m[32m    sales: "{{ url_for('admin_sales_report') }}",[m
     audit: "{{ url_for('admin_audit') }}",[m
     reports: "{{ url_for('admin_reports') }}",[m
     analytics: "{{ url_for('admin_analytics_page') }}",[m
[36m@@ -99,9 +99,9 @@[m
           <span class="text-2xl group-hover:scale-110 transition">⏱</span>[m
           <span class="text-xs font-medium text-gray-600 mt-1">Cron Jobs</span>[m
       </a>[m
[31m-      <a :href="urls.broadcast" class="flex flex-col items-center justify-center p-3 bg-white border rounded-lg hover:border-indigo-300 hover:shadow-md transition group">[m
[31m-          <span class="text-2xl group-hover:scale-110 transition">📣</span>[m
[31m-          <span class="text-xs font-medium text-gray-600 mt-1">Рассылка</span>[m
[32m+[m[32m      <a :href="urls.sales" class="flex flex-col items-center justify-center p-3 bg-white border rounded-lg hover:border-emerald-300 hover:shadow-md transition group">[m
[32m+[m[32m          <span class="text-2xl group-hover:scale-110 transition">💰</span>[m
[32m+[m[32m          <span class="text-xs font-medium text-gray-600 mt-1">Продажи</span>[m
       </a>[m
   </div>[m
 [m
[1mdiff --git a/templates/admin_user_detail.html b/templates/admin_user_detail.html[m
[1mindex dd3b086..87efe07 100644[m
[1m--- a/templates/admin_user_detail.html[m
[1m+++ b/templates/admin_user_detail.html[m
[36m@@ -18,7 +18,6 @@[m
      x-data="{[m
        tab: 'overview',[m
        isSubModalOpen: false,[m
[31m-       unlinkOpen: false,[m
 [m
        init() {[m
          this.$nextTick(() => {[m
[36m@@ -96,10 +95,6 @@[m
              <span class="bg-gray-100 text-gray-500 px-2 py-0.5 rounded text-xs" title="Устройство не зарегистрировано">🔕 No Push</span>[m
           {% endif %}[m
 [m
[31m-          {% if user.telegram_chat_id %}[m
[31m-             <span class="bg-blue-100 text-blue-700 px-2 py-0.5 rounded text-xs">✈️ Telegram</span>[m
[31m-          {% endif %}[m
[31m-[m
           {% if user.is_verified %}[m
              <span class="bg-teal-100 text-teal-700 px-2 py-0.5 rounded text-xs">Verified</span>[m
           {% endif %}[m
[36m@@ -433,27 +428,6 @@[m
       </div>[m
 [m
       <div class="space-y-6">[m
[31m-          <div class="bg-white p-6 rounded-xl shadow-sm border border-gray-100">[m
[31m-              <h3 class="text-lg font-bold mb-4">Telegram Bot</h3>[m
[31m-              {% if user.telegram_chat_id %}[m
[31m-                  <div class="p-3 bg-blue-50 text-blue-800 rounded-lg text-sm mb-4">[m
[31m-                      Привязан ID: <strong>{{ user.telegram_chat_id }}</strong>[m
[31m-                  </div>[m
[31m-                  <div class="flex flex-col gap-2">[m
[31m-                    <form method="post" action="{{ url_for('admin_user_send_test_tg', user_id=user.id) }}">[m
[31m-                        <button class="w-full px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">[m
[31m-                            Отправить тест[m
[31m-                        </button>[m
[31m-                    </form>[m
[31m-                    <button @click="unlinkOpen = true" class="w-full px-4 py-2 border border-red-200 text-red-600 rounded text-sm hover:bg-red-50">[m
[31m-                        Отвязать[m
[31m-                    </button>[m
[31m-                  </div>[m
[31m-              {% else %}[m
[31m-                  <div class="text-sm text-gray-500">Нет привязки.</div>[m
[31m-              {% endif %}[m
[31m-          </div>[m
[31m-[m
           <div class="bg-white p-6 rounded-xl shadow-sm border border-red-100">[m
               <h3 class="text-lg font-bold text-red-600 mb-4">Опасная зона</h3>[m
               <form action="{{ url_for('admin_delete_user', user_id=user.id) }}" method="POST"[m
[36m@@ -530,22 +504,8 @@[m
     </div>[m
   </div>[m
 [m
[31m-  <div x-show="unlinkOpen" x-cloak class="fixed inset-0 z-50 flex items-center justify-center p-4">[m
[31m-      <div class="absolute inset-0 bg-black/50" @click="unlinkOpen = false"></div>[m
[31m-      <div class="bg-white rounded-lg p-6 relative z-10 max-w-sm w-full">[m
[31m-          <h3 class="font-bold text-lg mb-2">Отвязать Telegram?</h3>[m
[31m-          <p class="text-gray-600 mb-4 text-sm">Бот перестанет присылать уведомления этому пользователю.</p>[m
[31m-          <div class="flex justify-end gap-2">[m
[31m-              <button @click="unlinkOpen = false" class="px-4 py-2 text-gray-600 hover:bg-gray-100 rounded">Отмена</button>[m
[31m-              <form method="post" action="{{ url_for('admin_user_unlink_telegram', user_id=user.id) }}">[m
[31m-                  <button class="px-4 py-2 bg-red-600 text-white hover:bg-red-700 rounded">Да, отвязать</button>[m
[31m-              </form>[m
[31m-          </div>[m
[31m-      </div>[m
   </div>[m
 [m
[31m-</div>[m
[31m-[m
 <style>[m
     [x-cloak] { display: none !important; }[m
 </style>[m
[1mdiff --git a/templates/profile.html b/templates/profile.html[m
[1mindex 18b008c..b8b7aea 100644[m
[1m--- a/templates/profile.html[m
[1m+++ b/templates/profile.html[m
[36m@@ -887,22 +887,6 @@[m [mfunction showSlide(index) {[m
 [m
 <div id="body-measure-card" class="card p-6">[m
        <div class="relative text-center">[m
[31m-    <div class="absolute top-0 right-0 z-10">[m
[31m-        <button[m
[31m-            onclick="openTelegramModal()"[m
[31m-            title="Привязать Telegram"[m
[31m-            aria-label="Привязать Telegram"[m
[31m-            class="tg-attn-btn relative p-2 rounded-full hover:bg-slate-100 transition-colors {% if user.telegram_chat_id %}linked{% endif %}">[m
[31m-[m
[31m-            <i class="bi bi-telegram tg-icon"></i>[m
[31m-[m
[31m-            {% if not user.telegram_chat_id %}[m
[31m-            <span class="tg-ping tg-ping--1"></span>[m
[31m-            <span class="tg-ping tg-ping--2"></span>[m
[31m-            {% else %}[m
[31m-            {% endif %}[m
[31m-        </button>[m
[31m-    </div>[m
 [m
 [m
     {% if user.avatar %}[m
[36m@@ -1064,17 +1048,6 @@[m [mfunction showSlide(index) {[m
     </div>[m
 [m
 [m
[31m-    {% if not user.telegram_chat_id %}[m
[31m-    <div class="card p-6 bg-blue-50 border-blue-200 text-center">[m
[31m-        <h3 class="font-bold text-blue-800">Получайте уведомления в Telegram</h3>[m
[31m-        <p class="text-sm text-blue-700 mt-1 mb-4">Привяжите аккаунт, чтобы получать диету и напоминания прямо в мессенджер.</p>[m
[31m-        <button onclick="openTelegramModal()" class="btn btn-primary w-full">[m
[31m-            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5 mr-2" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M15 10l-4 4l6 6l4 -16l-18 7l4 2l2 6l3 -4" /></svg>[m
[31m-            Привязать Telegram[m
[31m-        </button>[m
[31m-    </div>[m
[31m-    {% endif %}[m
[31m-[m
     <div class="card p-6 relative">[m
         <div {% if not current_user.has_subscription %}class="locked-feature"{% endif %}>[m
             <h3 class="text-lg font-bold mb-3 text-slate-800">Сообщество</h3>[m
[36m@@ -1681,89 +1654,6 @@[m [mfunction showSlide(index) {[m
   <p class="mt-4 text-lg font-semibold text-slate-700">Генерируем ваш идеальный рацион...</p>[m
   <p class="text-slate-500">Это может занять до 30 секунд.</p>[m
 </div>[m
[31m-[m
[31m-<div id="telegramModal" role="dialog" aria-modal="true" aria-labelledby="telegramModalTitle"[m
[31m-     class="fixed inset-0 bg-black/50 z-50 hidden items-center justify-center p-4"[m
[31m-     data-bot="{{ bot_username|default('TestKilogrBot') }}">[m
[31m-  <div class="card w-full max-w-2xl p-6 sm:p-8 relative animate-fade-in bg-white" onclick="event.stopPropagation()">[m
[31m-[m
[31m-    <button onclick="closeTelegramModal()" class="absolute top-4 right-4 text-slate-400 text-2xl hover:text-slate-600 transition-colors" aria-label="Закрыть">&times;</button>[m
[31m-    <div class="flex items-center gap-3 mb-4">[m
[31m-      <div class="h-10 w-10 rounded-xl bg-sky-100 flex items-center justify-center text-sky-600">✈️</div>[m
[31m-      <div>[m
[31m-        <h2 id="telegramModalTitle" class="text-lg sm:text-xl font-bold text-slate-900">Привязка Telegram</h2>[m
[31m-        <p class="text-slate-600 text-sm">Свяжите аккаунт, чтобы получать уведомления и делиться данными мгновенно.</p>[m
[31m-      </div>[m
[31m-    </div>[m
[31m-[m
[31m-    <div class="flex items-center gap-2 text-sm mb-5">[m
[31m-      <span class="relative flex h-2.5 w-2.5">[m
[31m-        <span class="absolute inline-flex h-full w-full rounded-full animate-ping bg-amber-400" id="tgStatusPing"></span>[m
[31m-        <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-500" id="tgStatusDot"></span>[m
[31m-      </span>[m
[31m-      <span id="tgStatusText" class="text-slate-600">Статус: ...</span>[m
[31m-      <span class="ml-auto text-xs text-slate-500">[m
[31m-        Бот: <a id="tgBotLink" class="font-medium text-blue-600 hover:underline" target="_blank" rel="noopener"><span id="tgBotName"></span></a>[m
[31m-      </span>[m
[31m-    </div>[m
[31m-[m
[31m-    <div id="tg-unlinked-view">[m
[31m-      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">[m
[31m-        <div class="space-y-4">[m
[31m-          <ol class="text-xs text-slate-500 space-y-2">[m
[31m-            <li class="flex items-center gap-2"><span class="inline-flex h-6 w-6 items-center justify-center rounded-full bg-slate-200 font-semibold">1</span> Откройте Telegram</li>[m
[31m-            <li class="flex items-center gap-2"><span class="inline-flex h-6 w-6 items-center justify-center rounded-full bg-slate-200 font-semibold">2</span> Найдите бота <span class="font-medium text-blue-600">@TestKilogrBot</span></li>[m
[31m-            <li class="flex items-center gap-2"><span class="inline-flex h-6 w-6 items-center justify-center rounded-full bg-slate-200 font-semibold">3</span> Отправьте ему ваш код</li>[m
[31m-          </ol>[m
[31m-          <div class="space-y-3">[m
[31m-            <div id="tgCodeDisplay" class="font-mono text-2xl tracking-wider select-all px-4 py-3 rounded-xl bg-slate-50 border border-slate-200">...</div>[m
[31m-            <div class="flex gap-3">[m
[31m-              <button onclick="copyCode()" class="btn btn-secondary w-full">📋 Скопировать</button>[m
[31m-              <a id="tgDeepLink" class="btn btn-primary w-full text-center" target="_blank" rel="noopener">Автопривязка</a>[m
[31m-            </div>[m
[31m-          </div>[m
[31m-        </div>[m
[31m-        <div class="rounded-xl border border-slate-200 p-4 bg-slate-50 flex flex-col items-center justify-center text-center">[m
[31m-            <div class="text-4xl mb-2">🔔</div>[m
[31m-            <h3 class="font-semibold text-slate-700">Настройки уведомлений</h3>[m
[31m-            <p class="text-xs text-slate-500 mt-1">После привязки вы сможете управлять оповещениями здесь.</p>[m
[31m-        </div>[m
[31m-      </div>[m
[31m-    </div>[m
[31m-[m
[31m-    <div id="tg-linked-view" class="hidden">[m
[31m-      <div class="rounded-xl border border-slate-200 p-4 space-y-3">[m
[31m-        <div class="flex items-center justify-between py-2 border-b border-slate-200">[m
[31m-          <div class="text-sm">[m
[31m-            <div class="font-medium text-slate-800">Получать уведомления в Telegram</div>[m
[31m-            <div class="text-slate-500 text-xs">Отключает/включает все уведомления разом</div>[m
[31m-          </div>[m
[31m-          <button id="tgSwitchGlobal" role="switch" aria-checked="false" data-key="telegram_notify_enabled" class="w-12 h-7 rounded-full bg-slate-300 relative transition">[m
[31m-              <span class="knob absolute top-0.5 left-0.5 h-6 w-6 rounded-full bg-white shadow transition"></span>[m
[31m-          </button>[m
[31m-        </div>[m
[31m-        <div class="flex items-center justify-between">[m
[31m-          <div class="text-sm"><div class="font-medium text-slate-800">Тренировки</div><div class="text-slate-500 text-xs">Напоминание за 1 час и в момент старта</div></div>[m
[31m-          <button id="tgSwitchTrainings" role="switch" aria-checked="true" data-key="notify_trainings" class="w-12 h-7 rounded-full bg-emerald-500 relative transition"><span class="knob absolute top-0.5 left-0.5 h-6 w-6 rounded-full bg-white shadow transition"></span></button>[m
[31m-        </div>[m
[31m-        <div class="flex items-center justify-between">[m
[31m-            <div class="text-sm"><div class="font-medium text-slate-800">Приёмы пищи</div><div class="text-slate-500 text-xs">в 8:00 · 12:00 · 18:00</div></div>[m
[31m-            <button id="tgSwitchMeals" role="switch" aria-checked="true" data-key="notify_meals" class="w-12 h-7 rounded-full bg-emerald-500 relative transition"><span class="knob absolute top-0.5 left-0.5 h-6 w-6 rounded-full bg-white shadow transition"></span></button>[m
[31m-        </div>[m
[31m-        <div class="flex items-center justify-between">[m
[31m-          <div class="text-sm"><div class="font-medium text-slate-800">Продление подписки</div><div class="text-slate-500 text-xs">Напоминание за 5 дней до окончания</div></div>[m
[31m-          <button id="tgSwitchSubscription" role="switch" aria-checked="true" data-key="notify_subscription" class="w-12 h-7 rounded-full bg-emerald-500 relative transition"><span class="knob absolute top-0.5 left-0.5 h-6 w-6 rounded-full bg-white shadow transition"></span></button>[m
[31m-        </div>[m
[31m-      </div>[m
[31m-       <div class="mt-4 text-center">[m
[31m-            <button onclick="unlinkTelegram()" class="text-sm font-medium text-red-500 hover:text-red-700 hover:underline">Отвязать аккаунт</button>[m
[31m-       </div>[m
[31m-    </div>[m
[31m-[m
[31m-  </div>[m
[31m-</div>[m
[31m-[m
[31m-[m
 <div id="editProfileModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm hidden items-center justify-center z-50 p-4" onclick="closeEditProfileModal()">[m
   <div class="card w-full max-w-lg p-6 sm:p-8 relative animate-fade-in" onclick="event.stopPropagation()">[m
     <button onclick="closeEditProfileModal()" class="absolute top-4 right-4 text-slate-400 text-2xl hover:text-slate-600">&times;</button>[m
[36m@@ -1829,9 +1719,37 @@[m [mfunction showSlide(index) {[m
             <button type="submit" class="btn btn-primary">Сменить пароль</button>[m
         </div>[m
     </form>[m
[32m+[m
[32m+[m[32m    <hr class="my-6 border-slate-200">[m
[32m+[m[32m    <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center bg-red-50 p-4 rounded-lg border border-red-200 gap-4">[m
[32m+[m[32m        <div>[m
[32m+[m[32m            <h3 class="text-red-800 font-bold text-sm">Опасная зона</h3>[m
[32m+[m[32m            <p class="text-xs text-red-600 mt-1">Удаление аккаунта навсегда сотрет все ваши данные и отменит подписки.</p>[m
[32m+[m[32m        </div>[m
[32m+[m[32m        <button onclick="deleteMyAccount()" class="btn bg-red-600 hover:bg-red-700 text-white text-sm shrink-0">Удалить аккаунт</button>[m
[32m+[m[32m    </div>[m
   </div>[m
 </div>[m
 [m
[32m+[m[32m<script>[m
[32m+[m[32mfunction deleteMyAccount() {[m
[32m+[m[32m    if (confirm("Вы абсолютно уверены? Это действие необратимо. Вся ваша история, фото и прогресс будут удалены.")) {[m
[32m+[m[32m        fetch('/api/me/delete', { method: 'POST' })[m
[32m+[m[32m        .then(res => res.json())[m
[32m+[m[32m        .then(data => {[m
[32m+[m[32m            if (data.ok) {[m
[32m+[m[32m                alert("Ваш аккаунт успешно удален.");[m
[32m+[m[32m                window.location.href = '/login';[m
[32m+[m[32m            } else {[m
[32m+[m[32m                alert("Ошибка удаления: " + data.error);[m
[32m+[m[32m            }[m
[32m+[m[32m        }).catch(err => {[m
[32m+[m[32m            alert("Сетевая ошибка. Не удалось удалить аккаунт.");[m
[32m+[m[32m        });[m
[32m+[m[32m    }[m
[32m+[m[32m}[m
[32m+[m[32m</script>[m
[32m+[m
 <style>[m
 /* Стили для табов модального окна */[m
 .meal-tab {[m
[36m@@ -2015,74 +1933,6 @@[m [mfunction showSlide(index) {[m
             });[m
     }[m
 [m
[31m-// --- Логика для модального окна Telegram ---[m
[31m-let tgPollTimer = null;[m
[31m-[m
[31m-// НОВАЯ ФУНКЦИЯ для переключения интерфейса на "привязанный" вид[m
[31m-function switchToLinkedView() {[m
[31m-    document.getElementById('tg-unlinked-view').classList.add('hidden');[m
[31m-    document.getElementById('tg-linked-view').classList.remove('hidden');[m
[31m-[m
[31m-    // Обновляем заголовок и статус[m
[31m-    const modalTitle = document.getElementById('telegramModalTitle');[m
[31m-    if (modalTitle) modalTitle.textContent = 'Управление Telegram';[m
[31m-    setTgStatusLinked();[m
[31m-    stopTgPolling();[m
[31m-[m
[31m-    // Загружаем актуальные настройки уведомлений[m
[31m-    loadTgSettings();[m
[31m-}[m
[31m-[m
[31m-// ИЗМЕНЕННАЯ ФУНКЦИЯ открытия модального окна[m
[31m-async function openTelegramModal() {[m
[31m-    // Сначала проверяем текущий статус[m
[31m-    const statusRes = await fetch('/api/me/telegram/status');[m
[31m-    const statusData = await statusRes.json();[m
[31m-[m
[31m-    const modal = document.getElementById('telegramModal');[m
[31m-    const bot = (modal?.dataset?.bot) || '';[m
[31m-[m
[31m-    // Настраиваем ссылки на бота в любом случае[m
[31m-    const botNameEl = document.getElementById('tgBotName');[m
[31m-    if (botNameEl) botNameEl.textContent = bot || '';[m
[31m-    const botLinkEl = document.getElementById('tgBotLink');[m
[31m-    if (botLinkEl && bot) botLinkEl.href = `https://t.me/${bot}`;[m
[31m-[m
[31m-    if (statusData && statusData.linked) {[m
[31m-        // Если уже привязан, просто показываем нужный интерфейс[m
[31m-        openModal('telegramModal');[m
[31m-        switchToLinkedView();[m
[31m-    } else {[m
[31m-        // Если не привязан, генерируем новый код[m
[31m-        const codeRes = await fetch('/generate_telegram_code');[m
[31m-        const codeData = await codeRes.json();[m
[31m-[m
[31m-        if (!codeData.code) { alert('Не удалось сгенерировать код.'); return; }[m
[31m-[m
[31m-        // Показываем блок для привязки[m
[31m-        document.getElementById('tg-linked-view').classList.add('hidden');[m
[31m-        document.getElementById('tg-unlinked-view').classList.remove('hidden');[m
[31m-[m
[31m-        // Возвращаем исходный заголовок[m
[31m-        const modalTitle = document.getElementById('telegramModalTitle');[m
[31m-        if (modalTitle) modalTitle.textContent = 'Привязка Telegram';[m
[31m-[m
[31m-        // Обновляем код и диплинк[m
[31m-        const codeEl = document.getElementById('tgCodeDisplay');[m
[31m-        if (codeEl) codeEl.textContent = codeData.code;[m
[31m-        const deepLinkEl = document.getElementById('tgDeepLink');[m
[31m-        if (deepLinkEl && bot) deepLinkEl.href = `https://t.me/${bot}?start=${encodeURIComponent(codeData.code)}`;[m
[31m-[m
[31m-        setTgStatusWaiting();[m
[31m-        openModal('telegramModal');[m
[31m-        startTgPolling(); // Запускаем проверку[m
[31m-    }[m
[31m-[m
[31m-    // Обработчики для переключателей вешаем один раз[m
[31m-    bindTgSwitches();[m
[31m-}[m
[31m-[m
[31m-[m
 // --- НОВОЕ: UI переключателей[m
 function renderSwitch(btn, isOn) {[m
   if (!btn) return;[m

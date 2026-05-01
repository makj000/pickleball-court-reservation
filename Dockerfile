FROM public.ecr.aws/lambda/python:3.12

# Install Chromium system dependencies (Amazon Linux 2023 uses dnf)
RUN dnf install -y \
    alsa-lib atk cups-libs gtk3 libdrm libXcomposite libXcursor \
    libXdamage libXext libXfixes libXi libXrandr libXrender libXtst \
    mesa-libgbm nss nspr pango \
    xorg-x11-fonts-100dpi xorg-x11-fonts-75dpi xorg-x11-fonts-cyrillic \
    xorg-x11-fonts-misc xorg-x11-fonts-Type1 xorg-x11-utils \
    && dnf clean all

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium to a fixed path accessible by any user
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium && chmod -R 777 /ms-playwright

COPY --chmod=644 monitor.py .
COPY --chmod=644 config.json .

CMD ["monitor.handler"]

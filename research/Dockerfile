FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates redis-server \
      texlive-latex-base texlive-latex-recommended \
      texlive-fonts-recommended texlive-publishers texlive-latex-extra \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work

# The subject system is pinned by full commit SHA. A shallow clone of the
# default branch would silently give a later revision than the one measured,
# so the commit is fetched explicitly and the checkout is verified.
ARG SUBJECT_COMMIT=38b201375926b11ff94703f6257a01aa8723c23d
RUN git init /root/github-autopilot \
 && git -C /root/github-autopilot remote add origin \
      https://github.com/Shweta-Mishra-ai/github-autopilot.git \
 && git -C /root/github-autopilot fetch --depth 1 origin "$SUBJECT_COMMIT" \
 && git -C /root/github-autopilot checkout --detach FETCH_HEAD \
 && test "$(git -C /root/github-autopilot rev-parse HEAD)" = "$SUBJECT_COMMIT" \
 && echo "subject pinned at $SUBJECT_COMMIT"

COPY . /work
ENV SUBJECT_REPO=/root/github-autopilot
CMD ["./reproduce.sh"]

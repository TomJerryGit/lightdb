-- ============================================================
-- LightDB 资料知识库
-- 兼容 MySQL 5.7 / LightDB
-- ============================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------- 1. 版本表 ----------
DROP TABLE IF EXISTS `lightdb_versions`;
CREATE TABLE `lightdb_versions` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `version`       VARCHAR(32) NOT NULL,
    `codename`      VARCHAR(64) DEFAULT '',
    `release_date`  DATE,
    `summary`       TEXT,
    `mysql_compat`  VARCHAR(32) DEFAULT '',
    `score`         DECIMAL(3,1) DEFAULT 0,
    `notes`         TEXT,
    UNIQUE KEY `uk_version` (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 2. 模块表 ----------
DROP TABLE IF EXISTS `lightdb_modules`;
CREATE TABLE `lightdb_modules` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `code`          VARCHAR(64) NOT NULL,
    `name`          VARCHAR(128) NOT NULL,
    `category`      VARCHAR(64) NOT NULL,
    `parent_id`     INT DEFAULT 0,
    `description`   TEXT,
    `layer`         VARCHAR(32) DEFAULT '',   -- kernel/studio/web/deploy
    `sort_order`    INT DEFAULT 0,
    UNIQUE KEY `uk_code` (`code`),
    KEY `idx_category` (`category`),
    KEY `idx_layer` (`layer`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 3. 文件表 ----------
DROP TABLE IF EXISTS `lightdb_files`;
CREATE TABLE `lightdb_files` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `module_id`     INT,
    `file_name`     VARCHAR(255) NOT NULL,
    `file_path`     VARCHAR(512) NOT NULL,
    `file_type`     VARCHAR(32) DEFAULT '',   -- .py/.pyx/.md/.yaml/.json
    `language`      VARCHAR(32) DEFAULT '',   -- python/cython/markdown/yaml
    `size_bytes`    INT DEFAULT 0,
    `lines`         INT DEFAULT 0,
    `summary`       TEXT,
    `status`        VARCHAR(16) DEFAULT 'provided',  -- provided/missing/fixed
    `has_issue`     TINYINT(1) DEFAULT 0,
    `duplicate_of`  VARCHAR(255) DEFAULT '',
    `created_at`    DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY `idx_module` (`module_id`),
    KEY `idx_status` (`status`),
    KEY `idx_type` (`file_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 4. 文档表 ----------
DROP TABLE IF EXISTS `lightdb_docs`;
CREATE TABLE `lightdb_docs` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `title`         VARCHAR(255) NOT NULL,
    `doc_type`      VARCHAR(32) NOT NULL,   -- release/guide/design/whitepaper
    `version`       VARCHAR(32) DEFAULT '',
    `content`       LONGTEXT,
    `summary`       TEXT,
    `file_id`       INT,
    `tags`          VARCHAR(255) DEFAULT '',
    `created_at`    DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY `idx_type` (`doc_type`),
    KEY `idx_version` (`version`),
    FULLTEXT KEY `ft_content` (`title`, `summary`, `content`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 5. 已知问题表 ----------
DROP TABLE IF EXISTS `lightdb_issues`;
CREATE TABLE `lightdb_issues` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `module_code`   VARCHAR(64) DEFAULT '',
    `file_name`     VARCHAR(255) DEFAULT '',
    `severity`      VARCHAR(16) NOT NULL,   -- critical/high/medium/low
    `category`      VARCHAR(32) DEFAULT '', -- bug/security/performance/design
    `title`         VARCHAR(255) NOT NULL,
    `description`   TEXT,
    `suggestion`    TEXT,
    `fixed`         TINYINT(1) DEFAULT 0,
    `fixed_in`      VARCHAR(32) DEFAULT '',
    `created_at`    DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY `idx_severity` (`severity`),
    KEY `idx_module` (`module_code`),
    KEY `idx_fixed` (`fixed`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 6. 特性矩阵 ----------
DROP TABLE IF EXISTS `lightdb_features`;
CREATE TABLE `lightdb_features` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `feature`       VARCHAR(128) NOT NULL,
    `category`      VARCHAR(64) DEFAULT '',
    `v1`            TINYINT(1) DEFAULT 0,
    `v2`            TINYINT(1) DEFAULT 0,
    `v3`            TINYINT(1) DEFAULT 0,
    `v4`            TINYINT(1) DEFAULT 0,
    `notes`         VARCHAR(255) DEFAULT '',
    UNIQUE KEY `uk_feature` (`feature`),
    KEY `idx_category` (`category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 7. 性能基准 ----------
DROP TABLE IF EXISTS `lightdb_benchmarks`;
CREATE TABLE `lightdb_benchmarks` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `scene`         VARCHAR(64) NOT NULL,
    `v3_value`      DECIMAL(15,2) DEFAULT 0,
    `v4_value`      DECIMAL(15,2) DEFAULT 0,
    `mysql57_value` DECIMAL(15,2) DEFAULT 0,
    `unit`          VARCHAR(32) DEFAULT '',
    `v3_ratio`      DECIMAL(6,2) DEFAULT 0,
    `v4_ratio`      DECIMAL(6,2) DEFAULT 0,
    `notes`         VARCHAR(255) DEFAULT '',
    UNIQUE KEY `uk_scene` (`scene`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 8. API 端点 ----------
DROP TABLE IF EXISTS `lightdb_api`;
CREATE TABLE `lightdb_api` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `module_code`   VARCHAR(64) DEFAULT '',
    `method`        VARCHAR(16) NOT NULL,
    `path`          VARCHAR(255) NOT NULL,
    `auth`          VARCHAR(32) DEFAULT '',
    `description`   VARCHAR(512) DEFAULT '',
    KEY `idx_module` (`module_code`),
    KEY `idx_path` (`path`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 9. 标签表 ----------
DROP TABLE IF EXISTS `lightdb_tags`;
CREATE TABLE `lightdb_tags` (
    `id`            INT PRIMARY KEY AUTO_INCREMENT,
    `name`          VARCHAR(64) NOT NULL,
    `color`         VARCHAR(16) DEFAULT '#569cd6',
    UNIQUE KEY `uk_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 10. 文件-标签多对多 ----------
DROP TABLE IF EXISTS `lightdb_file_tags`;
CREATE TABLE `lightdb_file_tags` (
    `file_id`       INT NOT NULL,
    `tag_id`        INT NOT NULL,
    PRIMARY KEY (`file_id`, `tag_id`),
    KEY `idx_tag` (`tag_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET FOREIGN_KEY_CHECKS = 1;